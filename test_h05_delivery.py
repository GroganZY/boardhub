"""Offline release tests. Every delivery uses a temporary executable or a fake socket."""

import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from test_board_watch_and_wake import HUB_DIR, FakeClock, board_watch, build_v2_md, make_uuid, wake_routes


class LaunchAgentTests(unittest.TestCase):
    def test_plist_matches_installer_bootstrap_command(self):
        installer = (HUB_DIR / "install.sh").read_text()
        plist_path = HUB_DIR / "local.boardhub-watch.plist"
        launch_args = plistlib.loads(plist_path.read_bytes())["ProgramArguments"]
        bootstrap = re.search(r'^    /usr/bin/python3 -I "\$runtime_dir/board_watch.py".*?--bootstrap',
                              installer, flags=re.M | re.S).group()
        command = shlex.split(bootstrap.replace("\\\n", ""))
        if "__BOARDHUB_BOARD_DIR__" in launch_args:
            settings = {"runtime_dir": "/tmp/runtime with spaces", "board_dir": "/tmp/board with spaces", "human_role": "owner"}
            # Exercise the embedded renderer without invoking the installer or launchctl.
            renderer = installer.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
            with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
                output = Path(tmp) / "rendered.plist"
                subprocess.run(
                    ["/usr/bin/python3", "-B", "-I", "-", str(plist_path), str(output), *settings.values()],
                    input=renderer, text=True, check=True, cwd=tmp,
                )
                launch_args = plistlib.loads(output.read_bytes())["ProgramArguments"]
        else:
            home_guard = re.search(r'\$\{HOME:-\} != (\S+)', installer).group(1)
            settings = {}
            for field in ("board_dir", "runtime_dir"):
                value = re.search(r'^' + field + r'="([^"]+)"$', installer, re.M).group(1)
                settings[field] = value.replace("$HOME", home_guard)
        expected = [re.sub(r'\$(\w+)', lambda m: settings[m.group(1)], arg) for arg in command[:-1]]
        self.assertEqual(command[-1], "--bootstrap")
        self.assertEqual(launch_args, expected)
        self.assertIn("--board", launch_args)
        self.assertIn("--runtime-dir", launch_args)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.project = self.root / "project with spaces"
        bridge = self.project / "bridge"
        self.board = bridge / "board"
        self.board.mkdir(mode=0o700, parents=True)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.capture = self.root / "captured.jsonl"
        self.clock = FakeClock(datetime(2026, 11, 26, 5, tzinfo=timezone.utc))
        self.fake = self.root / "test delivery program"
        self.fake_program()

    def fake_program(self, after_capture=""):
        self.fake.write_text(
            "#!/usr/bin/python3\nimport json, os, sys\n"
            f"with open({str(self.capture)!r}, 'a') as f:\n"
            "    f.write(json.dumps([os.getcwd(), sys.argv[1:]]) + '\\n')\n" + after_capture
        )
        self.fake.chmod(0o700)

    def captured(self):
        if not self.capture.exists():
            return []
        return [json.loads(line) for line in self.capture.read_text().splitlines()]

    def write_routes(self, routes=None, mail_enabled=False):
        cfg = {"version": 2, "routes": routes or {},
               "mail": {"enabled": mail_enabled, "notifier_path": str(self.fake)}}
        path = self.runtime / "routes.json"
        path.write_text(json.dumps(cfg))
        path.chmod(0o600)
        return cfg

    def publish(self, number, recipient, uuid, *, thread=None, prev="none", needs_user="no"):
        path = self.board / f"{number:04d}_gemini_to_{recipient}--{uuid}.md"
        path.write_bytes(build_v2_md("gemini", recipient, "info", uuid, thread or uuid, prev,
                                     needs_user=needs_user, body="PRIVATE MESSAGE BODY"))
        return path

    def tick(self, **kwargs):
        return board_watch.run_tick(self.board, self.runtime, now_fn=self.clock, **kwargs)

    def test_codex_and_both_claude_watchers_use_fixed_argv_and_retain_inboxes(self):
        codex_session, main_session, side_session = map(make_uuid, (601, 602, 603))
        self.write_routes({
            "codex": {"adapter": "codex", "enabled": True, "session_id": codex_session,
                      "sandbox": "read-only", "codex_bin": str(self.fake)},
            "claude_main": {"adapter": "claude", "enabled": True, "session_id": main_session,
                            "claude_bin": str(self.fake)},
            "claude_side": {"adapter": "claude", "enabled": True, "session_id": side_session,
                            "claude_bin": str(self.fake)},
        })
        old = make_uuid(600)
        self.publish(1, "codex", old)
        board_watch.bootstrap_runtime(self.board, self.runtime, as_of=self.clock())
        for number, agent in enumerate(("codex", "claude_main", "claude_side"), start=2):
            self.clock.advance(20)
            message = make_uuid(number)
            self.publish(number, agent, message)
            self.assertEqual(self.tick()["wake_sent"], {"agent": agent, "uuid": message, "outcome": "accepted"})
            self.assertIsNone(self.tick()["wake_sent"])
            self.assertIn(message, (self.board / "_hub" / f"inbox_{agent}.md").read_text())
        self.assertEqual(self.captured(), [
            [str(self.project.resolve()), ["exec", "-C", str(self.project.resolve()), "--sandbox", "read-only",
                                          "resume", "--skip-git-repo-check", codex_session,
                                          wake_routes.build_wake_prompt(make_uuid(2), "codex")]],
            [str(self.project.resolve()), ["-p", "--resume", main_session,
                                          wake_routes.build_wake_prompt(make_uuid(3), "claude_main")]],
            [str(self.project.resolve()), ["-p", "--resume", side_session,
                                          wake_routes.build_wake_prompt(make_uuid(4), "claude_side")]],
        ])
        self.assertIn(old, (self.board / "_hub" / "inbox_codex.md").read_text())

    def test_codex_queue_timeout_and_overflow_hold_the_attempt(self):
        cfg = self.write_routes({
            "codex": {"adapter": "codex", "enabled": True, "mode": "queue", "thread_id": make_uuid(20),
                      "project_dir": str(self.project), "codex_bin": str(self.fake)},
        })
        for number, failure, reason in (
            (21, "import time\ntime.sleep(10)\n", "outer_timeout"),
            (22, "sys.stderr.write('x' * 300000)\n", "output_limit_exceeded"),
        ):
            with self.subTest(reason=reason):
                self.fake_program(failure)
                message = make_uuid(number)
                with mock.patch.object(wake_routes, "OUTER_DEADLINE_SECONDS", 2), \
                        mock.patch.object(wake_routes, "_run_bounded", wraps=wake_routes._run_bounded) as child:
                    self.assertEqual(wake_routes.wake("codex", message, runtime_dir=self.runtime,
                                                      config=cfg, now_fn=self.clock), "uncertain")
                self.assertEqual(child.call_args.args[0][1:], [
                    "queue", "--thread", make_uuid(20), "--message", wake_routes.build_wake_prompt(message, "codex"),
                ])
                path = self.runtime / "wake_ledger.json"
                record = json.loads(path.read_text())["records"][f"codex:{message}"]
                self.assertEqual(record["last_reason_code"], reason)
                self.clock.advance(61)
                self.assertEqual(wake_routes.wake("codex", message, runtime_dir=self.runtime,
                                                  config=cfg, now_fn=self.clock), "uncertain")
                self.assertEqual(json.loads(path.read_text())["records"][f"codex:{message}"]["attempt_count"], 1)

    def test_dsh_frames_connect_retry_and_post_connect_failures(self):
        cfg = self.write_routes({
            "dsh": {"adapter": "dsh", "enabled": True, "session_id": "dsh_test_1",
                    "socket_path": str(self.root / "fake.sock")},
        })
        path = self.runtime / "wake_ledger.json"
        def wake(message):
            return wake_routes.wake("dsh", message, runtime_dir=self.runtime, config=cfg, now_fn=self.clock)

        client = mock.MagicMock(spec=socket.socket)
        client.__enter__.return_value = client
        response = b'{"ok":true,"response":{"jsonrpc":"2.0","id":1,"result":{"content":[]}}}\n'
        with mock.patch.object(wake_routes, "_check_broker_socket") as check, \
                mock.patch.object(wake_routes.socket, "socket", return_value=client):
            client.connect.side_effect = ConnectionRefusedError
            self.assertEqual(wake(make_uuid(30)), "not_sent")
            client.sendall.assert_not_called()
            self.clock.advance(61)
            client.connect.side_effect = None
            client.recv.side_effect = [response[:14], response[14:]]
            self.assertEqual(wake(make_uuid(30)), "accepted")
            check.assert_called_with(self.root / "fake.sock")
            wire = client.sendall.call_args.args[0]
            self.assertTrue(wire.endswith(b"\n"))
            self.assertEqual(json.loads(wire), {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "dsh_continue_session",
                           "arguments": {"sessionId": "dsh_test_1", "wait": False,
                                         "prompt": wake_routes.build_wake_prompt(make_uuid(30), "dsh")}},
            })
            for number, reply in enumerate((b"", b"x" * (wake_routes.MAX_CLIENT_OUTPUT_BYTES + 1),
                    response.replace(b'"content":[]', b'"content":[],"isError":true')), start=31):
                self.clock.advance(61)
                # Return chunks at the size the real socket would respect.
                chunks = io.BytesIO(reply)
                client.recv.side_effect = chunks.read
                message = make_uuid(number)
                self.assertEqual(wake(message), "uncertain")
                attempts = client.sendall.call_count
                self.clock.advance(61)
                self.assertEqual(wake(message), "uncertain")
                self.assertEqual(client.sendall.call_count, attempts)
        self.assertEqual(json.loads(path.read_text())["records"][f"dsh:{make_uuid(30)}"]["attempt_count"], 2)

    def test_probe_checks_disabled_routes_without_spawning_or_sending(self):
        self.write_routes({
            "gemini": {"adapter": "gemini_agy", "enabled": False, "conversation_id": "gemini_test",
                       "agy_client_path": str(self.fake)},
            "dsh": {"adapter": "dsh", "enabled": False, "session_id": "test_dsh",
                    "socket_path": str(self.root / "fake.sock")},
            "codex": {"adapter": "codex", "enabled": False, "mode": "queue", "thread_id": make_uuid(9),
                      "codex_bin": str(self.fake)},
            "claude_main": {"adapter": "claude", "enabled": False, "session_id": make_uuid(10),
                            "claude_bin": str(self.fake)},
            "claude_side": {"adapter": "claude", "enabled": False, "session_id": make_uuid(11),
                            "claude_bin": str(self.fake)},
        })
        client = mock.MagicMock(spec=socket.socket)
        client.__enter__.return_value = client
        out = io.StringIO()
        argv = ["probe", "--board", str(self.board), "--runtime-dir", str(self.runtime), "--skip-apfs-check"]
        with mock.patch.object(wake_routes, "_check_broker_socket"), \
                mock.patch.object(wake_routes.socket, "socket", return_value=client), \
                mock.patch.object(wake_routes.subprocess, "Popen", side_effect=AssertionError("probe spawned")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(board_watch.main(argv), 0)
        results = json.loads(out.getvalue())
        self.assertTrue(results["ok"])
        self.assertEqual(set(results["routes"]), {"gemini", "dsh", "codex", "claude_main", "claude_side"})
        self.assertEqual(client.connect.call_count, 2)
        client.sendall.assert_not_called()
        client.recv.assert_not_called()
        self.assertEqual(set(p.name for p in self.runtime.iterdir()), {"routes.json"})
        self.assertEqual(self.captured(), [])
        self.write_routes({"codex": {"adapter": "codex", "enabled": True, "session_id": "--malformed"}})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(board_watch.main(argv), 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "invalid_routes")

    def test_mail_defaults_and_three_event_types_with_metadata_only(self):
        example = json.loads((HUB_DIR / "routes.example.json").read_text())
        default = wake_routes.default_route_config()
        self.assertEqual(example["mail"]["enabled"], default["mail"]["enabled"])
        self.assertTrue(all(not route["enabled"] for route in example["routes"].values()))
        wake_routes.validate_route_config(example)

        self.write_routes(mail_enabled=True)
        historic = make_uuid(40)
        self.publish(1, "codex", historic, needs_user="yes")
        board_watch.bootstrap_runtime(self.board, self.runtime, as_of=self.clock())
        self.assertIsNone(self.tick(allow_wake=False, allow_mail=True)["mail_sent"])
        review = make_uuid(41)
        self.publish(2, "codex", review, needs_user="yes")
        head = thread = make_uuid(100)
        mutable = self.publish(3, "codex", thread)
        for number in range(101, 130):
            message = make_uuid(number)
            self.publish(number, "codex", message, thread=thread, prev=head)
            head = message
        for _ in range(2):
            self.clock.advance(20)
            self.assertEqual(self.tick(allow_wake=False, allow_mail=True)["mail_sent"]["outcome"], "sent")
        mutable.write_bytes(mutable.read_bytes() + b"untrusted mutation\n")
        self.clock.advance(20)
        self.assertEqual(self.tick(allow_wake=False, allow_mail=True)["mail_sent"]["outcome"], "sent")
        self.assertIsNone(self.tick(allow_wake=False, allow_mail=True)["mail_sent"])

        captured = [args for _, args in self.captured()]
        self.assertCountEqual([args[3] for args in captured], [
            f"Title: Human review requested\nMessage ID: {review}\nThread: {review}\n",
            f"Title: Thread paused at 30 messages\nMessage ID: {head}\nThread: {thread}\n",
            f"Title: High-severity lint error\nMessage ID: {thread}\nThread: {thread}\n",
        ])
        for args in captured:
            self.assertEqual(args[::2], ["--subject", "--body"])
            self.assertNotIn("PRIVATE MESSAGE BODY", " ".join(args))
            self.assertNotIn(str(self.board), " ".join(args))
        self.assertNotIn(str(historic), str(captured))

    def test_mail_opt_in_legacy_upgrade_interruption_and_no_retry(self):
        self.fake_program("sys.exit(1)\n")
        self.write_routes()
        board_watch.bootstrap_runtime(self.board, self.runtime, as_of=self.clock())
        ledger_path = self.runtime / "mail_ledger.json"
        # Simulate upgrading an existing ledger created before automatic mail.
        ledger_path.write_text('{"version":2,"submissions":[]}')
        old, ambiguous, interrupted = map(make_uuid, (201, 202, 203))
        self.publish(1, "codex", old, needs_user="yes")
        self.write_routes(mail_enabled=True)
        self.assertIsNone(self.tick()["mail_sent"])
        self.publish(2, "codex", ambiguous, needs_user="yes")
        self.assertEqual(self.tick()["mail_sent"]["outcome"], "uncertain")
        self.publish(3, "codex", interrupted, needs_user="yes")
        with mock.patch.object(wake_routes, "run_mail_notifier", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        self.clock.advance(600)
        self.assertIsNone(self.tick()["mail_sent"])
        self.assertEqual(len(self.captured()), 1)
        self.assertEqual([s["status"] for s in json.loads(ledger_path.read_text())["submissions"]],
                         ["uncertain", "uncertain"])
        self.write_routes(mail_enabled=False)
        self.publish(4, "codex", make_uuid(204), needs_user="yes")
        self.assertIsNone(self.tick()["mail_sent"])
        ledger_path.unlink()
        self.assertEqual(self.tick()["reason"], "runtime_corrupt")
        self.assertEqual(len(self.captured()), 1)

    def test_mail_defers_over_daily_budget_until_jst_midnight(self):
        self.write_routes(mail_enabled=True)
        board_watch.bootstrap_runtime(self.board, self.runtime, as_of=self.clock())
        first, second = make_uuid(301), make_uuid(302)
        self.publish(1, "codex", first, needs_user="yes")
        self.publish(2, "codex", second, needs_user="yes")
        with mock.patch.object(wake_routes, "MAIL_DAILY_ATTEMPT_CAP", 1):
            self.assertEqual(self.tick()["mail_sent"]["outcome"], "sent")
            self.assertIsNone(self.tick()["mail_sent"])
            self.clock.advance(10 * 3600)  # 05:00 UTC to 15:00 UTC = midnight JST.
            self.assertEqual(self.tick()["mail_sent"]["outcome"], "sent")
            self.assertEqual(len(self.captured()), 2)


if __name__ == "__main__":
    unittest.main()
