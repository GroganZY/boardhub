"""Claude Desktop wake tests using synthetic registries and sockets under /tmp."""

import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from test_board_watch_and_wake import FakeClock, board_lint, board_watch, build_v2_md, make_uuid, wake_routes


class ClaudePeerWakeTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.sessions = root / "sessions"
        self.sockets = root / "sockets"
        self.runtime = root / "runtime"
        self.board = root / "board"
        for directory in (self.sessions, self.sockets, self.runtime, self.board):
            directory.mkdir(mode=0o700)
        self.clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.desktop_ids = {role: f"local_{make_uuid(n)}" for n, role in enumerate(("claude_main", "claude_side"), 1)}
        self.token = "ab" * 16
        for name, value in (
            ("CLAUDE_PEER_SESSIONS", self.sessions),
            ("CLAUDE_PEER_SOCKET_DIRS", (self.sockets,)),
        ):
            patch = mock.patch.object(wake_routes, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        cli = mock.patch.object(wake_routes, "_run_cli_wake")
        self.addCleanup(cli.stop)
        self.addCleanup(cli.start().assert_not_called)

    def config(self):
        return {
            "version": 2,
            "routes": {
                role: {"adapter": "claude_peer", "enabled": True, "desktop_session_id": desktop_id}
                for role, desktop_id in self.desktop_ids.items()
            },
            "mail": {"enabled": False},
        }

    def register(self, role, cli_id, *, pid=None, **fields):
        pid = os.getpid() if pid is None else pid
        path = self.sockets / f"{pid}.sock"
        identity = {"procStart": "Thu Jan  1 00:00:00 2026", "pidDomain": "fixture"}
        metadata = self.sessions / f"{pid}.json"
        metadata.write_text(json.dumps({
            "hostSessionId": self.desktop_ids[role], "sessionId": cli_id,
            "pid": pid, "messagingSocketPath": str(path), "peerProtocol": 1,
            "entrypoint": "claude-desktop", "kind": "interactive",
            **identity, **fields,
        }), encoding="utf-8")
        socket_hash = hashlib.sha256(os.fsencode(path)).hexdigest()
        key = self.sessions / f"{pid}.{socket_hash}.key"
        board_lint.atomic_write_private_file(key, json.dumps({"peerToken": self.token, **identity}).encode())
        return path, key

    def listen(self, path):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(path))
        listener.listen()
        listener.settimeout(0.5)
        return listener

    def wake(self, request, role="claude_main"):
        return wake_routes.wake(role, request, runtime_dir=self.runtime, config=self.config(), now_fn=self.clock)

    def ledger_record(self, request):
        ledger, corrupt = wake_routes.WakeLedger.load(self.runtime / "wake_ledger.json", now=self.clock())
        self.assertFalse(corrupt)
        return ledger.get_record("claude_main", request)

    def test_exact_peer_frames_for_both_roles_and_ledger_cooldown_and_dedupe(self):
        listener = None
        for n, (role, status) in enumerate((("claude_main", "idle"), ("claude_side", "busy")), 10):
            with self.subTest(role=role):
                cli_id, request = make_uuid(n), make_uuid(n + 10)
                path, _ = self.register(role, cli_id, status=status)
                if listener is None:
                    listener = self.listen(path)
                self.assertEqual(self.wake(request, role), "accepted")
                channel, _ = listener.accept()
                with channel:
                    channel.settimeout(0.5)
                    with channel.makefile("rb") as reader:
                        frames = [json.loads(line) for line in reader]
                self.assertEqual(frames, [
                    {"type": "auth", "token": self.token},
                    {"type": "user", "session_id": cli_id, "from": "boardhub", "priority": "later",
                     "message": {"role": "user", "content":
                                 '<cross-session-message from="boardhub" from-name="BoardHub">\n'
                                 f"BoardHub: new message {request} for {role}; read bridge/board\n"
                                 "</cross-session-message>"}},
                ])
                with mock.patch.object(wake_routes, "_run_claude_peer_wake") as send:
                    self.assertEqual(self.wake(request, role), "accepted")
                    self.assertEqual(self.wake(make_uuid(n + 20), role), "not_sent")
                    send.assert_not_called()

    def test_probe_checks_metadata_without_connecting_and_accepts_disabled_placeholder(self):
        path, key = self.register("claude_main", make_uuid(10))
        self.listen(path)
        config = self.config()
        config["routes"]["claude_side"].update(enabled=False, desktop_session_id=None)
        board_lint.atomic_write_private_file(self.runtime / "routes.json", json.dumps(config).encode())
        args = ["probe", "--skip-apfs-check", "--board", str(self.board), "--runtime-dir", str(self.runtime)]
        with mock.patch.object(wake_routes.socket, "socket") as connection:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(board_watch.main(args), 0)
            statuses = {k: v["status"] for k, v in json.loads(output.getvalue())["routes"].items()}
            self.assertEqual(statuses, {"claude_main": "ok", "claude_side": "inbox_only"})
            key.chmod(0o644)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(board_watch.main(args), 2)
            self.assertEqual(json.loads(output.getvalue())["routes"]["claude_main"]["status"],
                             "prerequisite_unavailable")
        connection.assert_not_called()

    def test_refuses_cli_ids_and_peer_adapter_for_other_roles(self):
        config = self.config()
        config["routes"]["codex"] = config["routes"].pop("claude_side")
        with self.assertRaises(wake_routes.RouteConfigError):
            wake_routes.validate_route_config(config)
        config = self.config()
        config["routes"]["claude_main"]["desktop_session_id"] = make_uuid(10)
        with self.assertRaises(wake_routes.RouteConfigError):
            wake_routes.validate_route_config(config)

    def test_closed_socket_leaves_message_in_inbox_without_retry(self):
        path, _ = self.register("claude_main", make_uuid(10))
        self.listen(path).close()
        board_lint.atomic_write_private_file(self.runtime / "routes.json", json.dumps(self.config()).encode())
        board_watch.bootstrap_runtime(self.board, self.runtime, as_of=self.clock())
        board_watch.run_tick(self.board, self.runtime, now_fn=self.clock)
        self.clock.advance(1)
        request = make_uuid(20)
        sent_at = board_lint.format_utc_rfc3339(self.clock())
        (self.board / f"0001_codex_to_claude_main--{request}.md").write_bytes(
            build_v2_md("codex", "claude_main", "info", request, request, sent_at=sent_at),
        )
        tick = board_watch.run_tick(self.board, self.runtime, now_fn=self.clock)
        self.assertEqual(tick["wake_sent"], {"agent": "claude_main", "uuid": request, "outcome": "not_sent"})
        self.assertIn(request, (self.board / "_hub" / "inbox_claude_main.md").read_text(encoding="utf-8"))
        self.clock.advance(120)
        self.assertIsNone(board_watch.run_tick(self.board, self.runtime, now_fn=self.clock)["wake_sent"])
        self.assertEqual(self.ledger_record(request)["last_reason_code"], "claude_peer_unavailable")

    def test_ambiguous_or_missing_session_does_not_connect(self):
        path, _ = self.register("claude_main", make_uuid(10))
        self.listen(path)
        self.register("claude_main", make_uuid(11), pid=os.getpid() + 1)
        with mock.patch.object(wake_routes.socket, "socket") as connection:
            self.assertEqual(self.wake(make_uuid(20), "claude_main"), "not_sent")
            self.assertEqual(self.wake(make_uuid(21), "claude_side"), "not_sent")
        connection.assert_not_called()

    def test_kernel_pid_mismatch_sends_no_token_or_prompt(self):
        path, _ = self.register("claude_main", make_uuid(10), pid=os.getpid() + 1)
        listener = self.listen(path)
        request = make_uuid(20)
        self.assertEqual(self.wake(request), "not_sent")
        channel, _ = listener.accept()
        with channel:
            channel.settimeout(0.5)
            self.assertEqual(channel.recv(4096), b"")
        self.assertEqual(self.ledger_record(request)["last_reason_code"], "claude_peer_identity_mismatch")

    def test_write_timeout_is_uncertain_and_is_never_retried(self):
        path, _ = self.register("claude_main", make_uuid(10))
        self.listen(path)
        request = make_uuid(20)
        fake_client = mock.MagicMock()
        fake_client.sendall.side_effect = TimeoutError()
        with mock.patch.object(wake_routes.socket, "socket", return_value=fake_client), \
                mock.patch.object(wake_routes, "_claude_peer_pid", return_value=os.getpid()):
            self.assertEqual(self.wake(request), "uncertain")
            self.clock.advance(120)
            self.assertEqual(self.wake(request), "uncertain")
        fake_client.sendall.assert_called_once()
        record = self.ledger_record(request)
        self.assertEqual(record["last_reason_code"], "claude_peer_write_uncertain")
        self.assertEqual(record["attempt_count"], 1)
        self.assertFalse(record["transient_retryable"])


if __name__ == "__main__":
    unittest.main()
