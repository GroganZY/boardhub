import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch

import session_discovery as discovery


A = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
C = "cccccccc-cccc-4ccc-cccc-cccccccccccc"
D = "dddddddd-dddd-4ddd-dddd-dddddddddddd"
SECRET_VALUE = "fake-private-message-content"
ALLOWED = {"id", "label", "model", "effort", "status", "last_seen"}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bhd-", dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "project"
        self.now = datetime.now(timezone.utc)
        self.scanner = discovery.SessionDiscovery()

    def path(self, *parts):
        path = self.home.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, path, fields):
        path.write_text(json.dumps(fields) + "\n", encoding="utf-8")

    def rollout(self, session_id, *, date=None):
        date = date or self.now
        day = date.strftime("%Y/%m/%d")
        stamp = date.strftime("%Y-%m-%dT%H-%M-%S")
        return self.path(".codex", "sessions", day, f"rollout-{stamp}-{session_id}.jsonl")

    def scan(self):
        result = self.scanner.discover(home=self.home, project_dir=self.project)
        for rows in result.values():
            for row in rows:
                self.assertLessEqual(set(row), ALLOWED)
        self.assertNotIn(SECRET_VALUE, json.dumps(result))
        return result

    def test_claude_scopes_and_deduplicates_metadata(self):
        recent = self.now.timestamp() * 1000
        self.write(self.path(*discovery.CLAUDE, A, B, f"local_{A}.json"), {
            "cliSessionId": A, "sessionId": f"local_{A}", "title": "Desktop session",
            "cwd": str(self.root / "scratch"), "originCwd": str(self.project / "nested"),
            "model": "claude-opus-5-5", "effort": "high", "lastActivityAt": recent,
            "promptAppendSnapshot": SECRET_VALUE,
        })
        self.write(self.path(".claude", "sessions", "42.json"), {
            "sessionId": A, "cwd": str(self.project), "updatedAt": recent - 1000,
        })
        self.write(self.path(".claude", "sessions", "43.json"), {
            "sessionId": B, "cwd": str(self.project) + "-other", "name": SECRET_VALUE,
        })
        self.write(self.path(".claude", "sessions", "44.json"), {
            "sessionId": C, "name": "CLI session", "createdAt": recent - 10 * 60 * 1000,
        })
        self.write(self.path(".claude", "sessions", "45.json"), {
            "sessionId": D, "name": "Archived", "cwd": str(self.project),
            "updatedAt": recent - 1000, "status": "archived",
        })
        rows = self.scan()["claude_code"]
        self.assertEqual([r["id"] for r in rows], [A, D, C])
        self.assertEqual([r["status"] for r in rows], ["live", "stale", "idle"])
        self.assertEqual(rows[0]["label"], "Desktop session")
        self.assertEqual(rows[0]["model"], "claude-opus-5-5")
        self.assertEqual(rows[0]["effort"], "high")
        self.assertTrue(rows[2]["last_seen"].endswith("Z"))

    def test_codex_uses_only_index_and_bounded_rollout_metadata(self):
        index = self.path(".codex", "session_index.jsonl")
        index.write_text("\n".join(json.dumps(row) for row in [
            {"id": A, "thread_name": "Older", "updated_at": "2022-01-02T00:00:00Z"},
            {"id": A, "thread_name": "Indexed", "updated_at": self.now.isoformat()},
            {"id": A, "thread_name": "Out of order", "updated_at": "2022-01-02T00:00:00Z"},
            {"id": B, "thread_name": "Excluded project"},
        ]), encoding="utf-8")
        header = {"type": "session_meta", "payload": {
            "id": A, "cwd": str(self.root / "elsewhere"),
            "runtime_workspace_roots": [str(self.project)],
            "base_instructions": SECRET_VALUE,
        }}
        body = {"type": "response_item", "payload": {"content": SECRET_VALUE * 2500}}
        turn = {"type": "turn_context", "timestamp": self.now.isoformat(),
                "payload": {"model": "gpt-6.2-sol", "effort": "xhigh", "sandbox_policy": SECRET_VALUE}}
        rollout = self.rollout(A)
        rollout.write_text("\n".join(json.dumps(row) for row in (header, body, turn, {
            "type": "response_item", "payload": {"model": "false-positive", "title": SECRET_VALUE},
        })) + "\n", encoding="utf-8")
        self.write(self.rollout(B), {"type": "session_meta", "payload": {
            "id": B, "cwd": str(self.root / "elsewhere"),
        }})
        self.write(self.rollout(C, date=self.now - timedelta(days=20)), {
            "type": "session_meta", "payload": {"id": C, "cwd": str(self.project)},
        })
        # Do not even decode values of unknown fields inside otherwise eligible JSON.
        real_loads = json.loads

        def check_loads(data):
            self.assertNotIn(SECRET_VALUE.encode(), data)
            return real_loads(data)

        with patch.object(discovery.json, "loads", side_effect=check_loads):
            rows = self.scan()["codex"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], A)
        self.assertEqual(rows[0]["label"], "Indexed")
        self.assertEqual(rows[0]["model"], "gpt-6.2-sol")
        self.assertEqual(rows[0]["effort"], "xhigh")

    def test_codex_never_reads_old_turn_from_middle(self):
        header = {"type": "session_meta", "payload": {"id": A, "cwd": str(self.project)}}
        turn = {"type": "turn_context", "payload": {"model": "gpt-6.2-sol"}}
        body = {"type": "response_item", "payload": {"content": SECRET_VALUE * 2500}}
        self.rollout(A).write_text("\n".join(json.dumps(row) for row in (header, turn, body)),
                                   encoding="utf-8")
        row, = self.scan()["codex"]
        self.assertNotIn("model", row)
        self.assertEqual(row["label"], "#" + A[:8])

    def test_antigravity_falls_back_to_metadata_fields_at_tail(self):
        transcript = self.path(*discovery.AGY, "brain", A, ".system_generated", "logs", "transcript.jsonl")
        self.path(*discovery.AGY, "annotations", A + ".pbtxt").write_text(
            'title: "Synthetic \\"title\\""\nprompt: "' + SECRET_VALUE + '"\n', encoding="utf-8",
        )
        transcript.write_text("\n".join(json.dumps(row) for row in (
            {"type": "USER_REQUEST", "content": SECRET_VALUE * 1000},
            {"type": "PLANNER_RESPONSE", "model": "gemini-4-argon-medium", "created_at": self.now.isoformat(),
             "thinking": SECRET_VALUE},
            {"type": "USER_REQUEST", "model": "ignored", "content": SECRET_VALUE},
        )), encoding="utf-8")
        row, = self.scan()["antigravity"]
        self.assertEqual(row["label"], 'Synthetic "title"')
        self.assertEqual(row["model"], "gemini-4-argon")
        self.assertEqual(row["effort"], "medium")

    def start_broker(self, parts, reply):
        # A connected Unix socket pair also works in sandboxes that deny bind().
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(side_effect=lambda *_: client.close())
        connection.settimeout = client.settimeout
        connection.sendall = client.sendall
        connection.recv = client.recv
        self.enter_patch(patch.object(discovery.socket, "socket", return_value=connection))
        self.enter_patch(patch.object(
            discovery, "_socket_signature", side_effect=lambda _, candidate: parts if candidate == parts else None,
        ))
        requests = []

        def serve():
            with server as conn:
                conn.settimeout(3)
                with conn.makefile("rb") as stream:
                    requests.append(json.loads(stream.readline()))
                conn.sendall(json.dumps(reply).encode() + b"\n")

        thread = Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 4)
        return requests

    def enter_patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_antigravity_broker_supersedes_local_fallbacks(self):
        self.path(*discovery.AGY, "brain", A, "placeholder")
        requests = self.start_broker(discovery.AGY_BROKER, {"ok": True, "result": {"response": {
            "conversationMetadata": {
                "annotations": {"title": "Broker title"},
                "metadata": {"model": "gemini-4-carbon-low", "effort": "high"},
                "state": {"updateTime": self.now.isoformat(), "summary": SECRET_VALUE},
            },
        }}})
        with patch.object(discovery, "_agy_log", side_effect=AssertionError("unneeded transcript read")):
            row, = self.scan()["antigravity"]
            self.assertEqual(self.scan()["antigravity"], [row])
        self.assertEqual(requests, [{"op": "get_conversation_metadata", "args": {"conversation_id": A}}])
        self.assertEqual((row["label"], row["model"], row["effort"]), ("Broker title", "gemini-4-carbon", "high"))

    def test_dsh_uses_list_only_and_supports_structured_index(self):
        requests = self.start_broker(discovery.DSH_BROKER, {"ok": True, "response": {
            "jsonrpc": "2.0", "id": 1, "result": {
                "structuredContent": {"sessions": [
                    {"sessionId": "session-1", "cwd": str(self.project), "name": "DSH",
                     "createdAt": self.now.isoformat(), "messages": [SECRET_VALUE]},
                    {"sessionId": "session-2", "cwd": str(self.project) + "-other", "title": SECRET_VALUE},
                ]},
                "content": [{"type": "text", "text": SECRET_VALUE}],
            },
        }})
        row, = self.scan()["dsh"]
        self.assertEqual(self.scan()["dsh"], [row])
        self.assertEqual(requests, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "dsh_list_sessions", "arguments": {}}}])
        self.assertEqual(row["label"], "DSH")

    def test_dsh_ignores_nonindex_text_and_uses_json_text_index(self):
        requests = self.start_broker(discovery.DSH_BROKER, {
            "jsonrpc": "2.0", "id": 1, "result": {"content": [
                {"type": "text", "text": SECRET_VALUE},
                {"type": "text", "text": json.dumps({"sessions": [{"id": "session-1", "name": "DSH"}]})},
            ]},
        })
        row, = self.scan()["dsh"]
        self.assertEqual(row["id"], "session-1")
        self.assertEqual(len(requests), 1)

    def test_cache_invalidates_and_ignores_removed_files(self):
        source = self.path(".claude", "sessions", "42.json")
        self.write(source, {"sessionId": A, "name": "Before"})
        with patch.object(discovery, "_claude_record", wraps=discovery._claude_record) as reader:
            self.assertEqual(self.scan()["claude_code"][0]["label"], "Before")
            self.assertEqual(self.scan()["claude_code"][0]["label"], "Before")
            self.assertEqual(reader.call_count, 1)
            self.write(source, {"sessionId": A, "name": "After"})
            self.assertEqual(self.scan()["claude_code"][0]["label"], "After")
            self.assertEqual(reader.call_count, 2)
            source.unlink()
            self.assertEqual(self.scan()["claude_code"], [])
            self.assertEqual(reader.call_count, 2)

    def test_source_budget_and_failure_leave_other_sources_available(self):
        clock = 100.0
        reached_after_deadline = False

        def slow_claude(*args):
            nonlocal clock, reached_after_deadline
            yield discovery._session(A, title="Partial result")
            clock += 0.21
            discovery._check_deadline()
            reached_after_deadline = True

        rollout = self.rollout(B)
        self.write(rollout, {"type": "session_meta", "payload": {"id": B, "cwd": str(self.project)}})
        with patch.object(discovery.time, "monotonic", side_effect=lambda: clock), \
                patch.object(discovery, "_claude", side_effect=slow_claude), \
                patch.object(discovery, "_agy", side_effect=RuntimeError("unreadable source")):
            sessions = self.scanner.discover(home=self.home, project_dir=self.project, timeout=0.8)
        self.assertFalse(reached_after_deadline)
        self.assertEqual([row["id"] for row in sessions["claude_code"]], [A])
        self.assertEqual(sessions["antigravity"], [])
        self.assertEqual([row["id"] for row in sessions["codex"]], [B])

    def test_cli_ignores_outside_files_symlinks_and_nonregular_entries(self):
        unrelated = self.root / "other.json"
        self.write(unrelated, {"sessionId": B, "name": SECRET_VALUE})
        self.path(".claude", "sessions", "42.json").symlink_to(unrelated)
        os.link(unrelated, self.path(".claude", "sessions", "43.json"))
        os.mkfifo(self.path(".claude", "sessions", "44.json"))
        self.path(".codex", "sessions").symlink_to(self.root, target_is_directory=True)
        self.path(".claude", "projects", "ignored.jsonl").write_text(SECRET_VALUE, encoding="utf-8")
        self.path(".codex", "auth.json").write_text(SECRET_VALUE, encoding="utf-8")
        self.write(self.path(".claude", "sessions", "45.json"), {
            "sessionId": A, "cwd": str(self.project), "name": "token: synthetic-token-123",
        })
        result = subprocess.run([
            sys.executable, "-I", str(Path(discovery.__file__).resolve()), "--json",
            "--project-dir", str(self.project),
        ], env={**os.environ, "HOME": str(self.home)}, capture_output=True,
            text=True, check=True, cwd=self.root, timeout=5)
        self.assertEqual(result.stderr, "")
        self.assertNotIn(SECRET_VALUE, result.stdout)
        self.assertNotIn("synthetic-token-123", result.stdout)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["claude_code"][0]["label"], "#" + A[:8])
        self.assertEqual(parsed["codex"], [])
        self.assertEqual(parsed["dsh"], [])


if __name__ == "__main__":
    unittest.main()
