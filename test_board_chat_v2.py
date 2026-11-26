#!/usr/bin/env python3
"""Synthetic unittest suite for board_chat_v2, distinct inboxes, and static deliverables (Area 7)."""

import base64
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HUB_DIR = Path(__file__).resolve().parent


def _load(name):
    path = HUB_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


board_lint = _load("board_lint")
board_state = _load("board_state")
board_chat_v2 = _load("board_chat_v2")


def make_uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


class TagInspector(HTMLParser):
    def __init__(self, html_text: str):
        super().__init__()
        self.tags = []
        self.feed(html_text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class BoardChatV2AndDeliverablesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.board = Path(self.tmp.name) / "board"
        self.board.mkdir(mode=0o700)
        self.out = Path(self.tmp.name) / "board_chat_v2.html"

    def test_escaped_html_csp_dashboard_stale_pending_and_distinct_inboxes(self):
        # Task 1 assigned to claude_side, reviewed by codex; stale (>24h old)
        u_t1 = make_uuid(700)
        (self.board / f"0001_claude_main_to_claude_side--{u_t1}.md").write_text(
            "---\nv: 2\nfrom: claude_main\nto: claude_side\ntype: task\n"
            f"request_id: {u_t1}\nthread: {u_t1}\nprev: none\n"
            "sent_at: 2026-11-24T00:00:00Z\nneeds_user: no\n"
            "assignee: claude_side\nreviewer: codex\nkind: coordination\n"
            'task: "><script>alert(1)</script>\n---\n\n'
            "# <script>alert(2)</script>\n"
            "[bad](javascript:alert(3)) [good](https://example.com/?a=1&b=2)\n",
            encoding="utf-8",
        )
        # Delivery by claude_side to codex (review request in codex inbox!)
        u_c1, u_c2, u_d1 = make_uuid(701), make_uuid(702), make_uuid(703)
        for num, uid, prev, st in (
            (2, u_c1, u_t1, "claimed"),
            (3, u_c2, u_c1, "in_progress"),
        ):
            (self.board / f"{num:04d}_claude_side_to_claude_main--{uid}.md").write_text(
                "---\nv: 2\nfrom: claude_side\nto: claude_main\ntype: state\n"
                f"request_id: {uid}\nthread: {u_t1}\nprev: {prev}\n"
                "sent_at: 2026-11-24T01:00:00Z\nneeds_user: no\n"
                f"state: {st}\n---\n\nmoving to {st}\n",
                encoding="utf-8",
            )
        (self.board / f"0004_claude_side_to_codex--{u_d1}.md").write_text(
            "---\nv: 2\nfrom: claude_side\nto: codex\ntype: delivery\n"
            f"request_id: {u_d1}\nthread: {u_t1}\nprev: {u_c2}\n"
            "sent_at: 2026-11-24T02:00:00Z\nneeds_user: yes\n---\n\nready for review\n",
            encoding="utf-8",
        )
        # Separate message for claude_main only (must NOT appear in claude_side inbox)
        u_m1 = make_uuid(704)
        (self.board / f"0005_codex_to_claude_main--{u_m1}.md").write_text(
            "---\nv: 2\nfrom: codex\nto: claude_main\ntype: info\n"
            f"request_id: {u_m1}\nthread: {u_m1}\nprev: none\n"
            "sent_at: 2026-11-26T05:00:00Z\nneeds_user: no\n---\n\nfor main only\n",
            encoding="utf-8",
        )
        # Legacy ambiguous numeric ref
        (self.board / "0010_dsh_to_claude.md").write_text("---\nfrom: dsh\nto: claude\n---\nA", encoding="utf-8")
        (self.board / "0010_codex_to_claude.md").write_text("---\nfrom: codex\nto: claude\n---\nB", encoding="utf-8")
        (self.board / "0011_claude_to_all.md").write_text("---\nfrom: claude\nto: all\nre: 0010\n---\nC", encoding="utf-8")

        as_of = datetime(2026, 11, 26, 6, 0, 0, tzinfo=timezone.utc)
        res = board_chat_v2.generate(self.board, self.out, as_of=as_of)
        html_text = self.out.read_text(encoding="utf-8")

        # Verify distinct inboxes & assignments/review requests
        inboxes = res["state"]["inboxes"]
        self.assertEqual([i["uuid"] for i in inboxes["claude_side"]], [u_t1])
        self.assertEqual([i["uuid"] for i in inboxes["codex"]], [u_d1])
        self.assertEqual({i["uuid"] for i in inboxes["claude_main"]}, {u_c1, u_c2, u_m1})

        # Verify dashboard header, stale status, pending items, and ambiguous legacy note
        self.assertIn("STALE &gt;24h", html_text)
        self.assertIn("待 owner 处理", html_text)
        self.assertIn(f'data-filter-thread="{u_t1}"', html_text)
        self.assertIn('data-filter-inbox="claude_main"', html_text)
        self.assertIn('data-filter-inbox="claude_side"', html_text)
        self.assertIn("ambiguous numeric ref; unlinked", html_text)

        # Verify strict escaping and CSP
        self.assertNotIn("<script>alert(1)</script>", html_text)
        self.assertNotIn("<script>alert(2)</script>", html_text)
        tags = TagInspector(html_text).tags
        self.assertEqual(sum(tag == "script" for tag, _ in tags), 1)
        self.assertFalse(any(tag in ("iframe", "img", "object", "embed") for tag, _ in tags))
        for tag, attrs in tags:
            self.assertFalse(any(k.startswith("on") for k in attrs))
            if tag == "a":
                self.assertFalse(attrs["href"].startswith(("javascript:", "data:")))
        expected_hash = base64.b64encode(hashlib.sha256(board_chat_v2.JS.encode("utf-8")).digest()).decode("ascii")
        self.assertIn(f"'sha256-{expected_hash}'", html_text)

    def test_static_plist_shell_and_isolated_cli(self):
        plist = HUB_DIR / "local.boardhub-watch.plist"
        install_sh = HUB_DIR / "install.sh"
        uninstall_sh = HUB_DIR / "uninstall.sh"
        self.assertTrue((HUB_DIR / "README_v2_draft.md").is_file())
        self.assertTrue((HUB_DIR / "RUN.md").is_file())

        p_res = subprocess.run(["/usr/bin/plutil", "-lint", str(plist)], capture_output=True, text=True)
        self.assertEqual(p_res.returncode, 0, p_res.stderr)

        for sh in (install_sh, uninstall_sh):
            b_res = subprocess.run(["/bin/bash", "-n", str(sh)], capture_output=True, text=True)
            self.assertEqual(b_res.returncode, 0, b_res.stderr)

        # Isolated mode execution of board_lint and board_watch --bootstrap / --once on /tmp board
        u1 = make_uuid(800)
        (self.board / f"0001_claude_main_to_dsh--{u1}.md").write_text(
            "---\nv: 2\nfrom: claude_main\nto: dsh\ntype: info\n"
            f"request_id: {u1}\nthread: {u1}\nprev: none\n"
            "sent_at: 2026-11-26T05:00:00Z\nneeds_user: no\n---\n\nhello\n",
            encoding="utf-8",
        )
        runtime = Path(self.tmp.name) / "rt"
        runtime.mkdir(mode=0o700)
        lint_proc = subprocess.run(
            ["/usr/bin/python3", "-I", str(HUB_DIR / "board_lint.py"), "--board", str(self.board)],
            capture_output=True, text=True,
        )
        self.assertEqual(lint_proc.returncode, 0, lint_proc.stderr)

        boot_proc = subprocess.run(
            ["/usr/bin/python3", "-I", str(HUB_DIR / "board_watch.py"),
             "--board", str(self.board), "--runtime-dir", str(runtime), "--bootstrap", "--skip-apfs-check"],
            capture_output=True, text=True,
        )
        self.assertEqual(boot_proc.returncode, 0, boot_proc.stderr)

        once_proc = subprocess.run(
            ["/usr/bin/python3", "-I", str(HUB_DIR / "board_watch.py"),
             "--board", str(self.board), "--runtime-dir", str(runtime), "--once", "--skip-apfs-check"],
            capture_output=True, text=True,
        )
        self.assertEqual(once_proc.returncode, 0, once_proc.stderr)

        # Isolated CLI with --config and --human override
        cfg_file = Path(self.tmp.name) / "cfg.json"
        cfg_file.write_text(json.dumps({"board": str(self.board), "human": "maintainer"}), encoding="utf-8")
        os.chmod(cfg_file, 0o600)
        html_out = Path(self.tmp.name) / "custom_chat.html"
        chat_proc = subprocess.run(
            ["/usr/bin/python3", "-I", str(HUB_DIR / "board_chat_v2.py"),
             "--config", str(cfg_file), "--output", str(html_out)],
            capture_output=True, text=True,
        )
        self.assertEqual(chat_proc.returncode, 0, chat_proc.stderr)
        self.assertIn("待 maintainer 处理", html_out.read_text(encoding="utf-8"))

    def test_legacy_and_v2_needs_grogan_user_human_highlight_and_filterable(self):
        self.addCleanup(board_lint.configure_human_role, board_lint.DEFAULT_HUMAN_ROLE)
        board_lint.configure_human_role("grogan")
        (self.board / "0001_codex_to_all.md").write_text(
            "---\nfrom: codex\nto: all\nneeds_grogan: yes\n---\nlegacy grogan\n",
            encoding="utf-8",
        )
        (self.board / "0002_codex_to_all.md").write_text(
            "---\nfrom: codex\nto: all\nneeds_human: yes\n---\nlegacy human\n",
            encoding="utf-8",
        )
        (self.board / "0003_codex_to_all.md").write_text(
            "---\nfrom: codex\nto: all\nneeds_user: yes\n---\nlegacy user\n",
            encoding="utf-8",
        )
        u_v2_g = make_uuid(850)
        (self.board / f"0004_claude_main_to_grogan--{u_v2_g}.md").write_text(
            "---\nv: 2\nfrom: claude_main\nto: grogan\ntype: info\n"
            f"request_id: {u_v2_g}\nthread: {u_v2_g}\nprev: none\n"
            "sent_at: 2026-11-26T05:00:00Z\nneeds_grogan: yes\n---\n\nv2 grogan\n",
            encoding="utf-8",
        )

        board_chat_v2.generate(self.board, self.out, as_of=datetime(2026, 11, 26, 6, 0, 0, tzinfo=timezone.utc))
        html_text = self.out.read_text(encoding="utf-8")
        articles = [attrs for tag, attrs in TagInspector(html_text).tags if tag == "article"]
        self.assertEqual(len(articles), 4)
        for attrs in articles:
            self.assertIn("needs", attrs.get("class", "").split())
            self.assertEqual(attrs.get("data-human"), "yes")
        self.assertEqual(html_text.count('<span class="badge attention">需要 grogan</span>'), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
