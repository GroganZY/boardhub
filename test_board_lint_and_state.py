#!/usr/bin/env python3
"""Synthetic unittest suite for board_lint and board_state (Area 1-4)."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HUB_DIR = Path(__file__).resolve().parent


def _load(name):
    path = HUB_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


board_lint = _load("board_lint")
board_state = _load("board_state")


def make_uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


def sha_bytes(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_v2_md(
    sender: str,
    recipient: str,
    msg_type: str,
    req_id: str,
    thread_id: str,
    prev: str = "none",
    sent_at: str = "2026-11-26T03:00:00Z",
    needs_human: str = "no",
    extra_fields: dict = None,
    body: str = "Synthetic message body.",
) -> bytes:
    lines = [
        "---",
        "v: 2",
        f"from: {sender}",
        f"to: {recipient}",
        f"type: {msg_type}",
        f"request_id: {req_id}",
        f"thread: {thread_id}",
        f"prev: {prev}",
        f"sent_at: {sent_at}",
        f"needs_human: {needs_human}",
    ]
    for k, v in (extra_fields or {}).items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return ("\n".join(lines) + "\n").encode("utf-8")


def full_checklist(status: str = "checked", evidence_prefix: str = "verified item") -> dict:
    return {
        str(i): {"status": status, "evidence": f"{evidence_prefix} {i}"}
        for i in range(1, 22)
    }


class PublicationAndParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.board = Path(self.tmp.name) / "board"
        self.board.mkdir()
        self.as_of = datetime(2026, 11, 26, 10, 0, 0, tzinfo=timezone.utc)

    def test_concurrent_same_number_and_route_both_survive(self):
        u1, u2 = make_uuid(1), make_uuid(2)
        b1 = build_v2_md("claude_main", "dsh", "info", u1, u1, body="First writer")
        b2 = build_v2_md("claude_main", "dsh", "info", u2, u2, body="Second writer")
        r1 = board_state.publish_message(self.board, 142, "claude_main", "dsh", u1, b1)
        r2 = board_state.publish_message(self.board, 142, "claude_main", "dsh", u2, b2)
        self.assertEqual(r1["status"], "published")
        self.assertEqual(r2["status"], "published")
        self.assertTrue((self.board / r1["filename"]).exists())
        self.assertTrue((self.board / r2["filename"]).exists())

        snap = board_lint.scan_board(self.board)
        res = board_state.rebuild(snap, {}, self.as_of)
        self.assertEqual(res["lint"]["exit_code"], 0)
        codes = [d["code"] for d in res["lint"]["diagnostics"]]
        self.assertEqual(codes.count("NUMBER_COLLISION"), 2)
        self.assertEqual(res["state"]["counts"]["v2_accepted"], 2)

    def test_idempotent_retry_conflict_and_interrupted_publication(self):
        u1 = make_uuid(10)
        b1 = build_v2_md("claude_main", "dsh", "info", u1, u1, body="Payload A")
        res1 = board_state.publish_message(self.board, 1, "claude_main", "dsh", u1, b1)
        self.assertEqual(res1["status"], "published")

        # Exact retry succeeds idempotently
        res_retry = board_state.publish_message(self.board, 1, "claude_main", "dsh", u1, b1)
        self.assertEqual(res_retry["status"], "idempotent_ok")
        self.assertEqual(res_retry["filename"], res1["filename"])

        # Same UUID with differing bytes or route raises RequestConflictError without replacing original
        b_diff = build_v2_md("claude_main", "dsh", "info", u1, u1, body="Payload B")
        with self.assertRaises(board_state.RequestConflictError):
            board_state.publish_message(self.board, 2, "claude_main", "dsh", u1, b_diff)
        with self.assertRaises(board_state.RequestConflictError):
            b_route = build_v2_md("claude_main", "codex", "info", u1, u1, body="Payload A")
            board_state.publish_message(self.board, 1, "claude_main", "codex", u1, b_route)
        self.assertEqual((self.board / res1["filename"]).read_bytes(), b1)

        # Simulated file fsync failure raises DurabilityError and cleans up temp file
        u2 = make_uuid(11)
        b2 = build_v2_md("claude_main", "dsh", "info", u2, u2, body="Fail fsync")

        def fail_fsync(_fd):
            raise OSError(5, "Simulated EIO")

        with self.assertRaises(board_state.DurabilityError):
            board_state.publish_message(self.board, 3, "claude_main", "dsh", u2, b2, _fsync_file_fn=fail_fsync)
        leftover_tmps = [p.name for p in self.board.iterdir() if p.name.startswith(".tmp_")]
        self.assertEqual(leftover_tmps, [])
        self.assertFalse((self.board / f"0003_claude_main_to_dsh--{u2}.md").exists())

    def test_ignored_appledouble_hidden_and_unsafe_symlink(self):
        u1 = make_uuid(20)
        b1 = build_v2_md("claude_main", "dsh", "info", u1, u1)
        (self.board / f"0001_claude_main_to_dsh--{u1}.md").write_bytes(b1)
        (self.board / f"._0001_claude_main_to_dsh--{u1}.md").write_bytes(b"\x00\x05\x16\x07")
        (self.board / ".tmp_partial_hidden.md").write_bytes(b"incomplete")
        (self.board / "_hub").mkdir()
        (self.board / "_hub" / "state.json").write_text("{}", encoding="utf-8")

        # Symlink target outside board must NOT be followed
        secret = Path(self.tmp.name) / "outside_secret.txt"
        secret.write_text("DO_NOT_READ", encoding="utf-8")
        u_sym = make_uuid(21)
        (self.board / f"0002_claude_main_to_dsh--{u_sym}.md").symlink_to(secret)
        # Non-regular directory with message-shaped name
        (self.board / "0003_claude_main_to_dsh.md").mkdir()

        snap = board_lint.scan_board(self.board)
        self.assertTrue(snap.healthy)
        self.assertEqual(list(snap.entries.keys()), [f"0001_claude_main_to_dsh--{u1}.md"])
        self.assertIn(f"0002_claude_main_to_dsh--{u_sym}.md", snap.unsafe_entries)
        self.assertIn("0003_claude_main_to_dsh.md", snap.unsafe_entries)

        res = board_state.rebuild(snap, {}, self.as_of)
        self.assertEqual(res["lint"]["exit_code"], 1)
        high_codes = [d["code"] for d in res["lint"]["diagnostics"] if d["severity"] == "high"]
        self.assertEqual(high_codes, ["UNSAFE_ENTRY", "UNSAFE_ENTRY"])

    def test_parsing_rejects_bad_utf8_dup_keys_oversize_depth_routes_and_legacy_ambiguity(self):
        # Bad UTF-8
        u1 = make_uuid(30)
        (self.board / f"0001_claude_main_to_dsh--{u1}.md").write_bytes(b"\xff\xfe---\n")
        # Duplicate frontmatter key
        u2 = make_uuid(31)
        dup_fm = (
            f"---\nv: 2\nfrom: claude_main\nto: dsh\ntype: info\nrequest_id: {u2}\n"
            f"thread: {u2}\nprev: none\nsent_at: 2026-11-26T03:00:00Z\nneeds_human: no\n"
            f"needs_human: no\n---\n\nbody\n"
        ).encode("utf-8")
        (self.board / f"0002_claude_main_to_dsh--{u2}.md").write_bytes(dup_fm)
        # Duplicate JSON key and NaN inside delivers/review_of
        u3 = make_uuid(32)
        u_root = make_uuid(33)
        (self.board / f"0003_claude_main_to_dsh--{u_root}.md").write_bytes(
            build_v2_md("claude_main", "dsh", "task", u_root, u_root,
                        extra_fields={"assignee": "dsh", "reviewer": "codex", "kind": "code"})
        )
        dup_json = (
            f"---\nv: 2\nfrom: dsh\nto: codex\ntype: delivery\nrequest_id: {u3}\n"
            f"thread: {u_root}\nprev: {u_root}\nsent_at: 2026-11-26T03:01:00Z\nneeds_human: no\n"
            'delivers: {"a.py":"' + ("0" * 64) + '","a.py":"' + ("1" * 64) + '"}\n'
            f'checklist: {json.dumps(full_checklist())}\n---\n\nbody\n'
        ).encode("utf-8")
        (self.board / f"0004_dsh_to_codex--{u3}.md").write_bytes(dup_json)
        # Oversize > 64 KiB
        u4 = make_uuid(34)
        (self.board / f"0005_claude_main_to_dsh--{u4}.md").write_bytes(
            build_v2_md("claude_main", "dsh", "info", u4, u4, body="x" * (65 * 1024))
        )
        # Oversize frontmatter > 24 KiB
        u5 = make_uuid(35)
        (self.board / f"0006_dsh_to_codex--{u5}.md").write_bytes(
            build_v2_md("dsh", "codex", "delivery", u5, u_root, prev=u_root,
                        extra_fields={"checklist": json.dumps(full_checklist(evidence_prefix="e" * 5000))},
                        body="body")
        )
        # JSON depth > 6
        u6 = make_uuid(36)
        deep_json = '{"a":{"b":{"c":{"d":{"e":{"f":{"g":"x"}}}}}}}'
        (self.board / f"0007_dsh_to_codex--{u6}.md").write_bytes(
            build_v2_md("dsh", "codex", "delivery", u6, u_root, prev=u_root,
                        extra_fields={"delivers": deep_json}, body="body")
        )
        # Route mismatch
        u7 = make_uuid(37)
        (self.board / f"0008_claude_main_to_codex--{u7}.md").write_bytes(
            build_v2_md("claude_main", "dsh", "info", u7, u7, body="wrong route")
        )
        # 'all' as sender
        u8 = make_uuid(38)
        (self.board / f"0009_all_to_dsh--{u8}.md").write_bytes(
            build_v2_md("all", "dsh", "info", u8, u8, body="sender all")
        )
        # Legacy numeric ambiguity
        (self.board / "0050_dsh_to_claude.md").write_text("---\nfrom: dsh\nto: claude\n---\nA", encoding="utf-8")
        (self.board / "0050_codex_to_claude.md").write_text("---\nfrom: codex\nto: claude\n---\nB", encoding="utf-8")
        (self.board / "0051_claude_to_all.md").write_text("---\nfrom: claude\nto: all\nre: 0050\n---\nC", encoding="utf-8")

        snap = board_lint.scan_board(self.board)
        res = board_state.rebuild(snap, {}, self.as_of)
        self.assertEqual(res["lint"]["exit_code"], 1)
        by_file = {d["file"]: d["code"] for d in res["lint"]["diagnostics"] if d["severity"] != "warning"}
        self.assertEqual(by_file[f"0001_claude_main_to_dsh--{u1}.md"], "FORMAT")
        self.assertEqual(by_file[f"0002_claude_main_to_dsh--{u2}.md"], "FORMAT")
        self.assertEqual(by_file[f"0004_dsh_to_codex--{u3}.md"], "FORMAT")
        self.assertEqual(by_file[f"0005_claude_main_to_dsh--{u4}.md"], "FORMAT")
        self.assertEqual(by_file[f"0006_dsh_to_codex--{u5}.md"], "FORMAT")
        self.assertEqual(by_file[f"0007_dsh_to_codex--{u6}.md"], "FORMAT")
        self.assertEqual(by_file[f"0008_claude_main_to_codex--{u7}.md"], "ROUTE_MISMATCH")
        self.assertEqual(by_file[f"0009_all_to_dsh--{u8}.md"], "FORMAT")

        warn_codes = [(d["file"], d["code"]) for d in res["lint"]["diagnostics"] if d["severity"] == "warning"]
        self.assertIn(("0051_claude_to_all.md", "LEGACY_REF_AMBIGUOUS"), warn_codes)
        legacy_51 = [m for m in res["legacy_messages"] if m["filename"] == "0051_claude_to_all.md"][0]
        self.assertTrue(legacy_51["re_ambiguous"])
        self.assertIsNone(legacy_51["re_resolved"])


class ReplayTasksPausesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.board = Path(self.tmp.name) / "board"
        self.board.mkdir()
        self.as_of = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

    def _write_v2(self, num: int, sender: str, recipient: str, msg_type: str,
                  req_id: str, thread_id: str, prev: str = "none",
                  sent_at: str = "2026-11-26T03:00:00Z", needs_human: str = "no",
                  extra: dict = None, body: str = "body"):
        fname = f"{num:04d}_{sender}_to_{recipient}--{req_id}.md"
        raw = build_v2_md(sender, recipient, msg_type, req_id, thread_id, prev,
                          sent_at=sent_at, needs_human=needs_human, extra_fields=extra, body=body)
        (self.board / fname).write_bytes(raw)
        return fname

    def test_shuffled_enumeration_identical_and_out_of_order_child(self):
        u_root, u_c1, u_c2 = make_uuid(100), make_uuid(101), make_uuid(102)
        # Child arrives before parent/root
        self._write_v2(2, "dsh", "claude_main", "state", u_c1, u_root, prev=u_root,
                       sent_at="2026-11-26T03:01:00Z", extra={"state": "claimed"})
        snap1 = board_lint.scan_board(self.board)
        res1 = board_state.rebuild(snap1, {}, self.as_of)
        self.assertEqual(res1["lint"]["exit_code"], 0)
        self.assertEqual([d["code"] for d in res1["lint"]["diagnostics"]], ["DEFERRED_REF"])
        self.assertEqual(res1["state"]["counts"]["v2_accepted"], 0)

        # Now root and grandchild arrive
        self._write_v2(1, "claude_main", "dsh", "task", u_root, u_root,
                       extra={"assignee": "dsh", "reviewer": "codex", "kind": "code", "task": "T/1"})
        self._write_v2(3, "dsh", "claude_main", "state", u_c2, u_root, prev=u_c1,
                       sent_at="2026-11-26T03:02:00Z", extra={"state": "in_progress"})

        snap_full = board_lint.scan_board(self.board)
        res_a = board_state.rebuild(snap_full, {}, self.as_of)
        # Shuffle dict insertion order in Snapshot
        keys = list(snap_full.entries.keys())
        random.Random(42).shuffle(keys)
        snap_shuffled = board_lint.BoardSnapshot(
            healthy=True,
            board_dir=snap_full.board_dir,
            entries={k: snap_full.entries[k] for k in keys},
        )
        res_b = board_state.rebuild(snap_shuffled, {}, self.as_of)
        self.assertEqual(json.dumps(res_a["state"], sort_keys=True), json.dumps(res_b["state"], sort_keys=True))
        self.assertEqual(json.dumps(res_a["lint"], sort_keys=True), json.dumps(res_b["lint"], sort_keys=True))
        self.assertEqual(res_a["state"]["tasks"][u_root]["state"], "in_progress")

    def test_cycle_cross_thread_retry_alias_and_request_conflict(self):
        u_root = make_uuid(200)
        u_other = make_uuid(201)
        self._write_v2(1, "claude_main", "codex", "info", u_root, u_root)
        self._write_v2(2, "claude_main", "codex", "info", u_other, u_other)
        # Cycle: A <-> B
        ua, ub = make_uuid(202), make_uuid(203)
        self._write_v2(3, "claude_main", "codex", "info", ua, u_root, prev=ub)
        self._write_v2(4, "codex", "claude_main", "info", ub, u_root, prev=ua)
        # Cross-thread prev
        uc = make_uuid(204)
        self._write_v2(5, "codex", "claude_main", "info", uc, u_root, prev=u_other)
        # Identical retry alias under two display numbers
        u_alias = make_uuid(205)
        self._write_v2(6, "codex", "claude_main", "info", u_alias, u_root, prev=u_root, body="Same bytes")
        self._write_v2(7, "codex", "claude_main", "info", u_alias, u_root, prev=u_root, body="Same bytes")
        # Conflicting UUID with different bytes
        u_conf = make_uuid(206)
        self._write_v2(8, "codex", "claude_main", "info", u_conf, u_other, prev=u_other, body="Copy 1")
        self._write_v2(9, "codex", "claude_main", "info", u_conf, u_other, prev=u_other, body="Copy 2")

        snap = board_lint.scan_board(self.board)
        res = board_state.rebuild(snap, {}, self.as_of)
        diags = {(d["file"], d["code"]) for d in res["lint"]["diagnostics"]}
        self.assertIn((f"0003_claude_main_to_codex--{ua}.md", "REFERENCE"), diags)
        self.assertIn((f"0004_codex_to_claude_main--{ub}.md", "REFERENCE"), diags)
        self.assertIn((f"0005_codex_to_claude_main--{uc}.md", "REFERENCE"), diags)
        self.assertIn((f"0007_codex_to_claude_main--{u_alias}.md", "RETRY_ALIAS"), diags)
        self.assertIn((f"0008_codex_to_claude_main--{u_conf}.md", "REQUEST_CONFLICT"), diags)
        self.assertIn((f"0009_codex_to_claude_main--{u_conf}.md", "REQUEST_CONFLICT"), diags)
        # Collapsed to lexicographically first basename
        self.assertEqual(res["state"]["threads"][u_root]["head_uuid"], u_alias)
        self.assertIn(f"0006_codex_to_claude_main--{u_alias}.md",
                      [r["filename"] for r in res["v2_records"] if r["status"] == "accepted"])
        # Affected conflict thread is frozen
        self.assertTrue(res["state"]["threads"][u_other]["frozen"])

    def test_thread_fork_rollback_and_invalid_sibling_beside_valid(self):
        u_root, u_c1 = make_uuid(300), make_uuid(301)
        self._write_v2(1, "claude_main", "dsh", "task", u_root, u_root,
                       extra={"assignee": "dsh", "reviewer": "codex", "kind": "coordination"})
        # Invalid sibling (dsh -> codex state transition is wrong role + wrong transition) beside valid claim
        u_bad = make_uuid(302)
        self._write_v2(2, "dsh", "codex", "state", u_bad, u_root, prev=u_root, extra={"state": "in_progress"})
        self._write_v2(3, "dsh", "claude_main", "state", u_c1, u_root, prev=u_root, extra={"state": "claimed"})

        snap1 = board_lint.scan_board(self.board)
        res1 = board_state.rebuild(snap1, {}, self.as_of)
        # Invalid sibling rejected, valid successor accepted!
        self.assertEqual(res1["state"]["tasks"][u_root]["state"], "claimed")
        self.assertFalse(res1["state"]["threads"][u_root]["paused"])
        self.assertIn((f"0002_dsh_to_codex--{u_bad}.md", "ROLE"),
                      {(d["file"], d["code"]) for d in res1["lint"]["diagnostics"]})

        # Valid progress u_c2 advances to in_progress
        u_c2 = make_uuid(303)
        self._write_v2(4, "dsh", "claude_main", "state", u_c2, u_root, prev=u_c1, extra={"state": "in_progress"})
        res2 = board_state.rebuild(board_lint.scan_board(self.board), {}, self.as_of)
        self.assertEqual(res2["state"]["tasks"][u_root]["state"], "in_progress")

        # Late competing valid sibling at u_c1 (e.g. an info message at prev=u_c1) rolls projection back to u_c1!
        u_fork = make_uuid(304)
        self._write_v2(5, "codex", "dsh", "info", u_fork, u_root, prev=u_c1, body="Competing branch!")
        # And a descendant of u_c2 becomes deferred
        u_child = make_uuid(305)
        self._write_v2(6, "dsh", "claude_main", "info", u_child, u_root, prev=u_c2, body="Descendant")

        res3 = board_state.rebuild(board_lint.scan_board(self.board), {}, self.as_of)
        thr = res3["state"]["threads"][u_root]
        self.assertTrue(thr["paused"])
        self.assertEqual(thr["pause_reason"], "thread_fork")
        self.assertEqual(thr["head_uuid"], u_c1)
        self.assertEqual(sorted(thr["conflict_ids"]), sorted([u_c2, u_fork]))
        self.assertEqual(res3["state"]["tasks"][u_root]["state"], "claimed")
        self.assertEqual(len(res3["state"]["pending"]), 1)
        self.assertEqual(res3["state"]["pending"][0]["reason"], "thread_fork")
        diags3 = {(d["file"], d["code"]) for d in res3["lint"]["diagnostics"]}
        self.assertIn((f"0006_dsh_to_claude_main--{u_child}.md", "DEFERRED_REF"), diags3)

    def test_full_task_transitions_dsh_checklist_and_review_binding_no_file_open(self):
        # Reference a path that does NOT exist and would raise if opened
        fake_path = "nonexistent_dir/secret_trap.py"
        fake_digest = sha_bytes("virtual delivered content")
        u_root = make_uuid(400)
        u_claim = make_uuid(401)
        u_prog = make_uuid(402)
        u_blk = make_uuid(403)
        u_unblk = make_uuid(404)
        u_del1 = make_uuid(405)
        u_rev1 = make_uuid(406)
        u_del2 = make_uuid(407)
        u_rev2 = make_uuid(408)

        self._write_v2(1, "claude_main", "dsh", "task", u_root, u_root,
                       extra={"assignee": "dsh", "reviewer": "codex", "kind": "code", "task": "DSH/CODE"})
        self._write_v2(2, "dsh", "claude_main", "state", u_claim, u_root, prev=u_root, extra={"state": "claimed"})
        self._write_v2(3, "dsh", "claude_main", "state", u_prog, u_root, prev=u_claim, extra={"state": "in_progress"})
        self._write_v2(4, "dsh", "claude_main", "state", u_blk, u_root, prev=u_prog, extra={"state": "blocked"})
        self._write_v2(5, "dsh", "claude_main", "state", u_unblk, u_root, prev=u_blk, extra={"state": "in_progress"})

        # Mixed checked and not_applicable checklist items
        chk = full_checklist("checked")
        chk["17"] = {"status": "not_applicable", "evidence": "CPU only check"}
        delivers_json = json.dumps({fake_path: fake_digest})
        self._write_v2(6, "dsh", "codex", "delivery", u_del1, u_root, prev=u_unblk,
                       extra={"delivers": delivers_json, "checklist": json.dumps(chk)})

        # Stale review (mismatched digest) rejected while valid review not yet present
        u_stale = make_uuid(409)
        self._write_v2(7, "codex", "dsh", "review", u_stale, u_root, prev=u_del1,
                       extra={"review_of": json.dumps({"id": u_del1, "delivers": {fake_path: "f" * 64}}),
                              "verdict": "approve"})
        res_mid = board_state.rebuild(board_lint.scan_board(self.board), {}, self.as_of)
        self.assertEqual(res_mid["state"]["tasks"][u_root]["state"], "in_review")
        self.assertIn("STALE_REVIEW", [d["code"] for d in res_mid["lint"]["diagnostics"]])

        # Valid revise
        self._write_v2(8, "codex", "dsh", "review", u_rev1, u_root, prev=u_del1,
                       extra={"review_of": json.dumps({"id": u_del1, "delivers": {fake_path: fake_digest}}),
                              "verdict": "revise"})
        # Second delivery & approve
        d2 = {"a/b.py": sha_bytes("v2")}
        self._write_v2(9, "dsh", "codex", "delivery", u_del2, u_root, prev=u_rev1,
                       extra={"delivers": json.dumps(d2), "checklist": json.dumps(chk)})
        self._write_v2(10, "codex", "dsh", "review", u_rev2, u_root, prev=u_del2,
                       extra={"review_of": json.dumps({"id": u_del2, "delivers": d2}),
                              "verdict": "approve"})

        # Duplicate verdict attempt after terminal done -> THREAD_CLOSED
        u_dup_rev = make_uuid(410)
        self._write_v2(11, "codex", "dsh", "review", u_dup_rev, u_root, prev=u_rev2,
                       extra={"review_of": json.dumps({"id": u_del2, "delivers": d2}),
                              "verdict": "approve"})

        res = board_state.rebuild(board_lint.scan_board(self.board), {}, self.as_of)
        self.assertEqual(res["state"]["tasks"][u_root]["state"], "done")
        self.assertIn("THREAD_CLOSED", [d["code"] for d in res["lint"]["diagnostics"]])

    def test_checklist_missing_extra_blank_and_oversize(self):
        u_root = make_uuid(450)
        self._write_v2(1, "claude_main", "dsh", "task", u_root, u_root,
                       extra={"assignee": "dsh", "reviewer": "codex", "kind": "code"})
        u1, u2 = make_uuid(451), make_uuid(452)
        self._write_v2(2, "dsh", "claude_main", "state", u1, u_root, prev=u_root, extra={"state": "claimed"})
        self._write_v2(3, "dsh", "claude_main", "state", u2, u_root, prev=u1, extra={"state": "in_progress"})

        d = {"m.py": "a" * 64}
        chk_missing_21 = full_checklist()
        del chk_missing_21["21"]
        chk_extra = full_checklist()
        chk_extra["22"] = {"status": "checked", "evidence": "extra"}
        chk_blank = full_checklist()
        chk_blank["5"]["evidence"] = "   "
        chk_big = full_checklist()
        chk_big["3"]["evidence"] = "x" * 513

        for idx, chk in enumerate((chk_missing_21, chk_extra, chk_blank, chk_big), start=4):
            uid = make_uuid(450 + idx)
            self._write_v2(idx, "dsh", "codex", "delivery", uid, u_root, prev=u2,
                           extra={"delivers": json.dumps(d), "checklist": json.dumps(chk)})

        res = board_state.rebuild(board_lint.scan_board(self.board), {}, self.as_of)
        chk_errors = [d for d in res["lint"]["diagnostics"] if d["code"] == "CHECKLIST"]
        self.assertEqual(len(chk_errors), 4)
        self.assertEqual(res["state"]["tasks"][u_root]["state"], "in_progress")

    def test_loop_breaker_30_progress_vs_terminal_and_human_escalations(self):
        # Task A: 29 events + progress info at 30 -> pauses (loop_breaker_30); event 31 rejected
        u_a = make_uuid(500)
        self._write_v2(1, "claude_main", "codex", "task", u_a, u_a,
                       extra={"assignee": "codex", "reviewer": "claude_main", "kind": "coordination"})
        prev = u_a
        for i in range(2, 30):
            uid = make_uuid(500 + i)
            sender, recip = ("codex", "claude_main") if i % 2 == 0 else ("claude_main", "codex")
            self._write_v2(i, sender, recip, "info", uid, u_a, prev=prev, body=f"progress {i}")
            prev = uid
        # Event 30 (progress)
        u30 = make_uuid(530)
        self._write_v2(30, "codex", "claude_main", "info", u30, u_a, prev=prev, body="progress 30")
        # Event 31 rejected
        u31 = make_uuid(531)
        self._write_v2(31, "claude_main", "codex", "info", u31, u_a, prev=u30, body="progress 31")

        # Task B: reaches terminal done on event 30 -> retains done WITHOUT pausing!
        u_b = make_uuid(600)
        self._write_v2(101, "claude_main", "codex", "task", u_b, u_b,
                       extra={"assignee": "codex", "reviewer": "claude_main", "kind": "coordination"})
        ub1, ub2 = make_uuid(601), make_uuid(602)
        self._write_v2(102, "codex", "claude_main", "state", ub1, u_b, prev=u_b, extra={"state": "claimed"})
        self._write_v2(103, "codex", "claude_main", "state", ub2, u_b, prev=ub1, extra={"state": "in_progress"})
        prev_b = ub2
        for i in range(4, 29):
            uid = make_uuid(600 + i)
            self._write_v2(100 + i, "codex", "claude_main", "info", uid, u_b, prev=prev_b)
            prev_b = uid
        ub29 = make_uuid(629)
        self._write_v2(129, "codex", "claude_main", "delivery", ub29, u_b, prev=prev_b)
        ub30 = make_uuid(630)
        self._write_v2(130, "claude_main", "codex", "review", ub30, u_b, prev=ub29,
                       extra={"review_of": json.dumps({"id": ub29, "delivers": {}}), "verdict": "approve"})

        # Task C: closes task AND sets needs_human: yes on same event -> done AND pending opens!
        u_c = make_uuid(700)
        uc1, uc2, uc3 = make_uuid(701), make_uuid(702), make_uuid(703)
        self._write_v2(201, "claude_main", "codex", "task", u_c, u_c,
                       extra={"assignee": "codex", "reviewer": "none", "kind": "coordination"})
        self._write_v2(202, "codex", "claude_main", "state", uc1, u_c, prev=u_c, extra={"state": "claimed"})
        self._write_v2(203, "codex", "claude_main", "state", uc2, u_c, prev=uc1, extra={"state": "in_progress"})
        self._write_v2(204, "codex", "claude_main", "delivery", uc3, u_c, prev=uc2, needs_human="yes")

        # Task D: second revise pauses after recording verdict
        u_d = make_uuid(800)
        ud = [make_uuid(800 + i) for i in range(1, 8)]
        self._write_v2(301, "claude_main", "codex", "task", u_d, u_d,
                       extra={"assignee": "codex", "reviewer": "claude_main", "kind": "coordination"})
        self._write_v2(302, "codex", "claude_main", "state", ud[0], u_d, prev=u_d, extra={"state": "claimed"})
        self._write_v2(303, "codex", "claude_main", "state", ud[1], u_d, prev=ud[0], extra={"state": "in_progress"})
        self._write_v2(304, "codex", "claude_main", "delivery", ud[2], u_d, prev=ud[1])
        self._write_v2(305, "claude_main", "codex", "review", ud[3], u_d, prev=ud[2],
                       extra={"review_of": json.dumps({"id": ud[2], "delivers": {}}), "verdict": "revise"})
        self._write_v2(306, "codex", "claude_main", "delivery", ud[4], u_d, prev=ud[3])
        self._write_v2(307, "claude_main", "codex", "review", ud[5], u_d, prev=ud[4],
                       extra={"review_of": json.dumps({"id": ud[4], "delivers": {}}), "verdict": "revise"})

        res = board_state.rebuild(board_lint.scan_board(self.board), {}, self.as_of)
        # Task A
        self.assertTrue(res["state"]["threads"][u_a]["paused"])
        self.assertEqual(res["state"]["threads"][u_a]["pause_reason"], "loop_breaker_30")
        self.assertEqual(res["state"]["threads"][u_a]["event_count"], 30)
        self.assertIn((f"0031_claude_main_to_codex--{u31}.md", "THREAD_CLOSED"),
                      {(d["file"], d["code"]) for d in res["lint"]["diagnostics"]})
        # Task B
        self.assertEqual(res["state"]["tasks"][u_b]["state"], "done")
        self.assertFalse(res["state"]["threads"][u_b]["paused"])
        self.assertEqual(res["state"]["threads"][u_b]["event_count"], 30)
        # Task C
        self.assertEqual(res["state"]["tasks"][u_c]["state"], "done")
        self.assertTrue(res["state"]["threads"][u_c]["paused"])
        self.assertEqual(res["state"]["threads"][u_c]["pause_reason"], "needs_human")
        # Task D
        self.assertEqual(res["state"]["tasks"][u_d]["state"], "in_progress")
        self.assertEqual(res["state"]["tasks"][u_d]["revise_count"], 2)
        self.assertTrue(res["state"]["threads"][u_d]["paused"])
        self.assertEqual(res["state"]["threads"][u_d]["pause_reason"], "second_revise")

        # Housekeeping hides one pending_id while keeping pause & history intact
        pending_ids = {p["thread_id"]: p["pending_id"] for p in res["state"]["pending"]}
        self.assertEqual(set(pending_ids.keys()), {u_a, u_c, u_d})
        hk_path = Path(self.tmp.name) / "housekeeping.json"
        board_state.add_housekeeping_receipt(hk_path, pending_ids[u_a], note="handled", as_of=self.as_of)
        hk_ids, _, _ = board_state.load_housekeeping_receipts(hk_path)
        res_hk = board_state.rebuild(board_lint.scan_board(self.board), {}, self.as_of, housekeeping_ids=hk_ids)
        self.assertEqual({p["thread_id"] for p in res_hk["state"]["pending"]}, {u_c, u_d})
        self.assertTrue(res_hk["state"]["threads"][u_a]["paused"])

        # Linked follow-up task with supersedes (valid for paused u_a, rejected for open task)
        u_followup = make_uuid(900)
        self._write_v2(400, "claude_main", "codex", "task", u_followup, u_followup,
                       extra={"assignee": "codex", "reviewer": "none", "kind": "coordination", "supersedes": u_a})
        res_sup = board_state.rebuild(board_lint.scan_board(self.board), {}, self.as_of)
        self.assertEqual(res_sup["state"]["tasks"][u_followup]["supersedes"], u_a)
        self.assertTrue(res_sup["state"]["threads"][u_a]["paused"])

    def test_configurable_human_role_and_config_resolution(self):
        self.addCleanup(board_lint.configure_human_role, board_lint.DEFAULT_HUMAN_ROLE)
        cfg_path = Path(self.tmp.name) / "boardhub.json"
        cfg_path.write_text(
            json.dumps({
                "board": str(self.board),
                "runtime_dir": str(Path(self.tmp.name) / "rt"),
                "human": "maintainer",
            }),
            encoding="utf-8",
        )
        os.chmod(cfg_path, 0o600)

        class DummyArgs:
            config = cfg_path
            board = None
            runtime_dir = None
            human = None

        bdir, rdir, hrole = board_lint.resolve_cli_settings(DummyArgs())
        self.assertEqual(bdir, self.board)
        self.assertEqual(rdir, Path(self.tmp.name) / "rt")
        self.assertEqual(hrole, "maintainer")
        self.assertIn("maintainer", board_state.get_inbox_recipients())

        u_task = make_uuid(950)
        u_ask = make_uuid(951)
        self._write_v2(1, "claude_main", "codex", "task", u_task, u_task,
                       extra={"assignee": "codex", "reviewer": "none", "kind": "coordination"})
        self._write_v2(2, "codex", "maintainer", "question", u_ask, u_task, prev=u_task, needs_human="yes")
        res = board_state.rebuild(board_lint.scan_board(self.board), {}, self.as_of)
        self.assertEqual(res["lint"]["exit_code"], 0)
        self.assertEqual(len(res["state"]["inboxes"]["maintainer"]), 1)
        self.assertTrue(res["state"]["threads"][u_task]["paused"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
