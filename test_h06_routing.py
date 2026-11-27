"""H06 tests with a synthetic board, fake CLI, mocked broker and mocked mail."""
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import time
import unittest
from unittest import mock

from test_board_watch_and_wake import (
    FakeClock, board_lint, board_state, board_watch, build_v2_md, make_uuid, wake_routes,
)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.board = self.root / "board"
        self.board.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.mail = mock.Mock(return_value=("sent", "sent"))
        self.agy = mock.Mock(return_value=("accepted", "accepted"))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(wake_routes, "run_mail_notifier", self.mail).start()
        mock.patch.object(wake_routes, "run_agy_client_send", self.agy).start()
        self.routes = {}

    def config(self, *, mail=False, **settings):
        config = {"version": 2, "routes": self.routes, "mail": {
            "enabled": mail, "notifier_path": str(self.root / "fake-notifier.py"),
        }, **settings}
        self.write_json(self.runtime / "routes.json", config)

    @staticmethod
    def write_json(path, data):
        board_lint.atomic_write_private_file(path, json.dumps(data).encode())

    def message(self, n, to, *, sender="claude_main", kind="info", session=None,
                thread=None, prev="none", file_sender=None):
        uid = make_uuid(n)
        sent = board_lint.format_utc_rfc3339(self.clock())
        fields = {"session_id": session} if session is not None else None
        raw = build_v2_md(sender, to, kind, uid, thread or uid, prev=prev,
                          sent_at=sent, extra_fields=fields, body="PRIVATE_BOARD_BODY")
        path = self.board / f"{n:04d}_{file_sender or sender}_to_{to}--{uid}.md"
        path.write_bytes(raw)
        return uid, path

    def control(self, n, role, session=None):
        return self.message(n, "all", sender=role, kind="checkout" if session is None else "checkin", session=session)

    def bootstrap(self):
        board_watch.bootstrap_runtime(self.board, self.runtime, as_of=self.clock())

    def tick(self, **opts):
        return board_watch.run_tick(self.board, self.runtime, now_fn=self.clock, **opts)

    def registry(self):
        return json.loads((self.runtime / "registry.json").read_text())["roles"]

    def record(self, role, uid):
        return json.loads((self.runtime / "wake_ledger.json").read_text())["records"].get(f"{role}:{uid}")

    def test_lint_rejects_sender_mismatch_garbage_and_duplicate_ids(self):
        _, spoofed = self.message(1, "all", kind="checkin", session=make_uuid(101), file_sender="claude_side")
        _, junk = self.control(2, "claude_main", "wrong-format")
        _, first = self.control(3, "claude_main", make_uuid(102))
        _, duplicate = self.control(4, "claude_side", make_uuid(102))
        _, same_role = self.control(5, "claude_main", make_uuid(102))
        _, extra = self.message(6, "all", kind="checkout", session=make_uuid(103))
        _, duplicate_key = self.control(7, "gemini", "session")
        duplicate_key.write_bytes(duplicate_key.read_bytes().replace(b"session_id: session", b"session_id: A\nsession_id: B"))
        _, source = self.control(8, "gemini", "session")
        alias = self.board / source.name.replace("0008_", "0009_", 1)
        alias.write_bytes(source.read_bytes())
        result = board_state.rebuild(board_lint.scan_board(self.board), {}, self.clock())
        by_file = {d["file"]: d["code"] for d in result["lint"]["diagnostics"]}
        self.assertEqual(by_file, {
            spoofed.name: "ROUTE_MISMATCH", junk.name: "FORMAT", duplicate.name: "DUPLICATE_SESSION_ID",
            same_role.name: "DUPLICATE_SESSION_ID", extra.name: "FORMAT", duplicate_key.name: "FORMAT",
            source.name: "REQUEST_CONFLICT", alias.name: "REQUEST_CONFLICT",
        })
        self.assertEqual(result["registry_events"]["claude_main"]["filename"], first.name)
        self.assertEqual(result["state"]["counts"]["v2_accepted"], 1)
        self.assertEqual(result["state"]["threads"][make_uuid(8)]["timeline"], [])
        self.assertTrue(all(not items for items in result["state"]["inboxes"].values()))

    def test_latest_checkin_checkout_and_pin_at_dispatch(self):
        route = {"adapter": "gemini_agy", "enabled": True, "conversation_id": "manual"}
        self.routes["gemini"] = route
        self.config()
        self.bootstrap()
        self.message(10, "gemini")
        self.tick()
        self.assertEqual(self.agy.call_args.kwargs["conversation_id"], "manual")
        self.clock.advance(61)
        self.control(11, "gemini", "older")
        newest, _ = self.control(12, "gemini", "registered")
        self.message(13, "gemini")
        self.tick()
        self.assertEqual(self.agy.call_args.kwargs["conversation_id"], "registered")
        reg_path = self.runtime / "registry.json"
        self.assertEqual(stat.S_IMODE(reg_path.stat().st_mode), 0o600)
        record = self.registry()["gemini"]
        self.assertEqual(record["event_id"], newest)
        self.assertEqual(record["checked_in_at"], record["last_successful_wake_at"])
        self.assertNotIn("registered", (self.board / "_hub" / "state.json").read_text())

        self.clock.advance(61)
        uid, _ = self.message(14, "gemini")
        # A checkout after selection must invalidate the wake on the same tick.
        result = self.tick(_pre_wake_hook=lambda: self.control(15, "gemini"))
        self.assertIsNone(result["wake_sent"])
        self.assertEqual(self.agy.call_count, 2)
        self.assertIsNone(self.registry()["gemini"]["session_id"])
        self.assertIn(uid, (self.board / "_hub" / "inbox_gemini.md").read_text())

        route["pin"] = True
        self.config()
        self.tick()
        self.assertEqual(self.agy.call_args.kwargs["conversation_id"], "manual")
        self.assertIsNone(self.registry()["gemini"]["last_successful_wake_at"])

    def test_success_refreshes_staleness_and_alert_attempt_is_once(self):
        self.routes["gemini"] = {"adapter": "gemini_agy", "enabled": True, "conversation_id": "fallback"}
        self.config(mail=True, stale_days=1)
        event, _ = self.control(20, "gemini", "private-session")
        self.bootstrap()
        self.clock.advance(80000)
        self.message(21, "gemini")
        self.assertEqual(self.tick()["wake_sent"]["outcome"], "accepted")
        self.clock.advance(86399)
        self.tick()
        self.mail.assert_not_called()
        self.clock.advance(1)
        self.message(22, "gemini")
        result = self.tick()
        self.assertIsNone(result["wake_sent"])
        self.assertEqual(self.agy.call_count, 1)
        item = self.mail.call_args.args[1]
        self.assertEqual(item["reason_code"], "route_stale")
        self.assertEqual(item["message_id"], event)
        self.assertNotIn("private-session", json.dumps(item))
        for _ in range(2):
            self.clock.advance(61)
            self.tick()
        self.mail.assert_called_once()

    def test_immutable_removal_does_not_resurrect_old_route(self):
        self.routes["gemini"] = {"adapter": "gemini_agy", "enabled": True, "conversation_id": "manual"}
        self.config()
        self.control(23, "gemini", "older")
        self.clock.advance(61)
        newest, path = self.control(24, "gemini", "latest")
        self.bootstrap()
        path.unlink()
        self.clock.advance(61)
        self.message(25, "gemini")
        self.assertIsNone(self.tick()["wake_sent"])
        self.agy.assert_not_called()
        record = self.registry()["gemini"]
        self.assertEqual(record["event_id"], newest)
        self.assertFalse(record["confirmed"])

    def test_interrupted_codex_reservation_releases_slot_without_retry(self):
        self.routes["codex"] = {"adapter": "codex", "enabled": True, "mode": "spawn"}
        self.config()
        self.bootstrap()
        first, _ = self.message(26, "codex")
        second, _ = self.message(27, "codex")
        ledger, corrupt = wake_routes.WakeLedger.load(self.runtime / "wake_ledger.json")
        self.assertFalse(corrupt)
        ledger.begin_attempt("codex", first, self.clock(), spawn={
            "adapter": "codex", "thread_id": first, "target": "0" * 64,
        })
        self.clock.advance(61)
        with mock.patch.object(wake_routes, "_trusted_program", return_value=self.root / "fake"):
            with mock.patch.object(wake_routes, "_start_codex_job", return_value=("accepted", "spawn_started")) as start:
                self.assertEqual(self.tick()["wake_sent"]["uuid"], second)
                start.assert_called_once()
        self.assertEqual(self.record("codex", first)["status"], "uncertain")
        self.assertEqual(self.record("codex", first)["attempt_count"], 1)

    def test_dsh_fresh_sessions_resume_and_validate_broker_result(self):
        self.routes["dsh"] = {"adapter": "dsh", "enabled": True, "mode": "spawn", "cwd": str(self.root),
                              "socket_path": str(self.root / "broker.sock"), "permissionPreset": "review"}
        self.config()
        self.bootstrap()
        client = mock.MagicMock(spec=socket.socket)
        client.__enter__.return_value = client

        def reply(result):
            client.recv.return_value = json.dumps({"ok": True, "response": {
                "jsonrpc": "2.0", "id": 1, "result": result,
            }}).encode() + b"\n"

        def sent():
            return json.loads(client.sendall.call_args.args[0])["params"]

        with mock.patch.object(wake_routes.socket, "socket", return_value=client), \
                mock.patch.object(wake_routes, "_check_broker_socket"):
            reply({"structuredContent": {"sessionId": "first-session"}})
            root, _ = self.message(30, "dsh")
            self.tick()
            self.assertEqual(sent(), {"name": "dsh_run_prompt", "arguments": {
                "cwd": str(self.root), "permissionPreset": "review", "wait": False,
                "prompt": f"BoardHub: new message {root}; read bridge/board",
            }})
            self.assertEqual(self.record("dsh", root)["spawn"]["session_id"], "first-session")
            self.clock.advance(61)
            reply({"content": []})
            second, _ = self.message(31, "dsh", thread=root, prev=root)
            self.tick()
            self.assertEqual(sent(), {"name": "dsh_continue_session", "arguments": {
                "sessionId": "first-session", "wait": False,
                "prompt": f"BoardHub: new message {second}; read bridge/board",
            }})
            self.clock.advance(61)
            reply({"content": [{"type": "text", "text": '{"sessionId":"new-thread-session"}'}]})
            third, _ = self.message(32, "dsh")
            self.tick()
            self.assertEqual(sent()["name"], "dsh_run_prompt")
            self.assertEqual(self.record("dsh", third)["spawn"]["session_id"], "new-thread-session")
            self.routes["dsh"]["resume_same_thread"] = False
            self.config()
            self.clock.advance(61)
            reply({"structuredContent": {"sessionId": "garbage id"}})
            fourth, _ = self.message(33, "dsh", thread=third, prev=third)
            self.tick()
            self.assertEqual(sent()["name"], "dsh_run_prompt")
            self.assertEqual(self.record("dsh", fourth)["status"], "uncertain")
            self.clock.advance(61)
            self.tick()
            self.assertEqual(client.sendall.call_count, 4)

    def test_codex_detaches_queues_and_resumes_after_receipt(self):
        fake = self.root / "fake-codex"
        fake.write_text('''#!/usr/bin/python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
uid = args[-1].split()[3].rstrip(';')
Path('argv-' + uid).write_text(json.dumps({'args': args, 'pid': os.getpid(), 'sid': os.getsid(0)}))
session = args[-2] if 'resume' in args else uid
print(json.dumps({'type': 'thread.started', 'thread_id': session}), flush=True)
end = time.monotonic() + 8
while not Path('release-' + uid).exists() and time.monotonic() < end:
    time.sleep(0.01)
print('PRIVATE_CODEX_OUTPUT', flush=True)
sys.exit(0 if Path('release-' + uid).exists() else 1)
''')
        fake.chmod(0o700)
        self.routes["codex"] = {"adapter": "codex", "enabled": True, "mode": "spawn", "codex_bin": str(fake),
                                "project_dir": str(self.root), "model": "model-test", "effort": "high",
                                "service_tier": "priority", "sandbox": "read-only"}
        self.config()
        self.bootstrap()
        root, _ = self.message(40, "codex")
        tick = self.tick()
        self.assertEqual(tick["wake_sent"]["outcome"], "accepted")
        self.assertEqual(self.record("codex", root)["status"], "attempting")

        def wait_for(predicate):
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if predicate():
                    return
                time.sleep(0.01)
            self.fail("fake Codex did not produce the expected receipt")

        receipt = self.runtime / f"codex-spawn-{root}.jsonl"
        wait_for(lambda: receipt.exists() and root in receipt.read_text())
        invoked = json.loads((self.root / f"argv-{root}").read_text())
        self.assertNotEqual(invoked["sid"], os.getsid(0))
        self.assertEqual(invoked["args"], [
            "exec", "-C", str(self.root), "--sandbox", "read-only", "--json", "--model", "model-test",
            "-c", 'model_reasoning_effort="high"', "-c", 'service_tier="priority"', "--skip-git-repo-check",
            f"BoardHub: new message {root}; read bridge/board",
        ])
        self.clock.advance(61)
        follow, _ = self.message(41, "codex", thread=root, prev=root)
        newer, _ = self.message(42, "codex")
        with mock.patch.object(wake_routes, "_CODEX_WORKERS", []):
            self.assertIsNone(self.tick()["wake_sent"])
        running = self.record("codex", root)["spawn"]
        self.assertEqual(running["pid"], invoked["pid"])
        self.assertEqual(running["session_id"], root)
        self.assertIsNone(self.record("codex", follow))
        self.assertIsNone(self.record("codex", newer))
        (self.root / f"release-{root}").touch()
        wait_for(lambda: not wake_routes._read_codex_receipt(receipt)[1])
        self.assertEqual(self.tick()["wake_sent"]["uuid"], follow)
        self.assertEqual(self.record("codex", root)["status"], "accepted")
        self.assertFalse(receipt.exists())
        follow_receipt = self.runtime / f"codex-spawn-{follow}.jsonl"
        wait_for(lambda: follow_receipt.exists() and root in follow_receipt.read_text())
        args = json.loads((self.root / f"argv-{follow}").read_text())["args"]
        self.assertEqual(args[-4:], ["resume", "--skip-git-repo-check", root,
                                     f"BoardHub: new message {follow}; read bridge/board"])
        (self.root / f"release-{follow}").touch()
        wait_for(lambda: not wake_routes._read_codex_receipt(follow_receipt)[1])
        self.assertNotIn("PRIVATE_CODEX_OUTPUT", follow_receipt.read_text())
        self.clock.advance(61)
        (self.root / f"release-{newer}").touch()
        self.assertEqual(self.tick()["wake_sent"]["uuid"], newer)
        newer_receipt = self.runtime / f"codex-spawn-{newer}.jsonl"
        wait_for(lambda: newer_receipt.exists() and "returncode" in newer_receipt.read_text())
        wait_for(lambda: not wake_routes._read_codex_receipt(newer_receipt)[1])
        self.tick()
        self.assertEqual(self.record("codex", newer)["spawn"]["session_id"], newer)

    def test_probe_accepts_registry_only_and_spawn_routes_with_fake_prerequisites(self):
        self.routes = {
            "gemini": {"adapter": "gemini_agy", "enabled": True},
            "codex": {"adapter": "codex", "enabled": True, "mode": "spawn"},
            "dsh": {"adapter": "dsh", "enabled": True, "mode": "spawn"},
        }
        self.config()
        with mock.patch.object(wake_routes, "_probe_socket") as socket_probe, \
                mock.patch.object(wake_routes, "_trusted_program"), \
                mock.patch.object(wake_routes, "_validate_executable_file"), \
                mock.patch.object(wake_routes.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(board_watch.main(["probe", "--skip-apfs-check", "--board", str(self.board),
                                               "--runtime-dir", str(self.runtime)]), 0)
        statuses = {r: v["status"] for r, v in json.loads(stdout.getvalue())["routes"].items()}
        self.assertEqual(statuses, {"codex": "ok", "dsh": "ok", "gemini": "awaiting_checkin"})
        self.assertEqual(socket_probe.call_count, 2)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
