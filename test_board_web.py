"""BoardHub Web tests. Run on temporary boards with /usr/bin/python3."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.client import HTTPConnection
import json
from pathlib import Path
import socket
import stat
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlsplit

import board_web as web

lint, state = web.board_lint, web.board_state


class EmbeddedScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = {}
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = dict(attrs).get("id", "inline")
            self.scripts[self.current] = ""

    def handle_endtag(self, tag):
        if tag == "script":
            self.current = None

    def handle_data(self, data):
        if self.current:
            self.scripts[self.current] += data


class BoardWebTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="boardhub-web-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.board = self.root / "board"
        self.runtime = self.root / "runtime"
        self.board.mkdir()
        self.server = web.create_server(self.board, self.runtime, port=0, display_name="协作者")
        self.addCleanup(self.server.server_close)
        self.runner = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.runner.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.runner.join(2)
        self.assertFalse(self.runner.is_alive())

    def request(self, method, path, headers=None, payload=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        # Exercise the real HTTP parser and handler without requiring a TCP
        # client connection. A separate test checks the live loopback bind.
        client, server = socket.socketpair()
        client.settimeout(10)
        self.server.process_request(server, ("127.0.0.1", 0))
        connection.sock = client
        try:
            connection.request(method, path, body=payload, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def login(self):
        url = self.runtime / "web_url.txt"
        target = urlsplit(url.read_text().strip())
        status, headers, _ = self.request("GET", target.path + "?" + target.query)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        status, html_headers, body = self.request("GET", "/", cookie)
        self.assertEqual(status, 200)
        parsed = EmbeddedScripts()
        parsed.feed(body.decode())
        self.assertIn("sha256-" + web.digest(parsed.scripts["inline"]), html_headers["Content-Security-Policy"])
        csrf = json.loads(parsed.scripts["board-data"])["csrf"]
        self.assertNotIn(target.query[2:].encode(), body)
        self.assertEqual(stat.S_IMODE(url.stat().st_mode), 0o600)
        return {**cookie, web.CSRF_HEADER: csrf}

    def get_changes(self, auth, since=""):
        status, _, body = self.request("GET", "/api/changes?since=" + since, auth)
        self.assertEqual(status, 200)
        return json.loads(body)

    def post(self, auth, expected=201, **fields):
        headers = {**auth, "Content-Type": "application/json"}
        status, _, result = self.request("POST", "/api/message", headers, json.dumps(fields).encode())
        self.assertEqual(status, expected, result)
        return json.loads(result)

    def replay(self):
        return web.data.read_board(self.board, self.runtime, datetime.now(timezone.utc))

    def seed(self, sender="gemini", to="user", needs="no", body="Synthetic request.", kind="question"):
        msg_id = str(web.uuid4())
        at = lint.format_utc_rfc3339(datetime.now(timezone.utc))
        raw = (f"---\nv: 2\nfrom: {sender}\nto: {to}\ntype: {kind}\nrequest_id: {msg_id}\n"
               f"thread: {msg_id}\nprev: none\nsent_at: {at}\nneeds_user: {needs}\n---\n\n{body}\n").encode()
        result = state.publish_message(self.board, None, sender, to, msg_id, raw)
        return msg_id, result["filename"]

    def test_loopback_ephemeral_fallback_and_shutdown(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertGreater(self.server.server_port, 0)
        another = self.root / "another-runtime"
        with web.create_server(self.board, another, self.server.server_port) as server:
            self.assertGreater(server.server_port, self.server.server_port)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertNotEqual(self.server.token, server.token)

    def test_host_cookie_and_csrf_on_every_api(self):
        auth = self.login()
        for host in ("example.test", "localhost.example.test", "127.0.0.1:12345", "127.0.0.1@evil.test"):
            headers = {**auth, "Host": host}
            self.assertEqual(self.request("GET", "/", headers)[0], 403)
            self.assertEqual(self.request("POST", "/api/message", headers)[0], 403)
        self.assertEqual(self.request("GET", "/", {**auth, "Host": "localhost"})[0], 200)
        self.assertEqual(self.request("GET", "/?t=wrong")[0], 401)
        self.assertEqual(self.request("GET", "/api/changes", {web.CSRF_HEADER: auth[web.CSRF_HEADER]})[0], 401)
        self.assertEqual(self.request("GET", "/api/changes", {"Cookie": auth["Cookie"]})[0], 403)
        self.assertEqual(self.request("GET", "/api/file", auth)[0], 404)
        self.assertEqual(self.request("POST", "/api/message", {**auth, web.CSRF_HEADER: "bad"})[0], 403)
        self.assertEqual(self.request("POST", "/api/message", auth, "{}")[0], 415)
        self.assertEqual(list(self.board.iterdir()), [])

    def test_changes_and_thread_reply(self):
        auth = self.login()
        before = self.get_changes(auth)
        self.assertEqual(before["reset"]["messages"], [])
        self.assertFalse(self.get_changes(auth, before["version"])["changed"])
        root, filename = self.seed(to="user")
        chunk = self.get_changes(auth, before["version"])
        self.assertEqual([row["id"] for row in chunk["upsert"]], [root])
        original = (self.board / filename).read_bytes()
        with patch.object(state, "publish_message", wraps=state.publish_message) as publisher:
            reply = self.post(auth, action="send", thread=root, type="info", to="gemini", body="Reply.")
            self.assertEqual(publisher.call_count, 1)
            self.assertIsNone(publisher.call_args.args[1])
        self.assertEqual((self.board / filename).read_bytes(), original)
        self.assertTrue(reply["path"].startswith("0002_"))
        self.assertEqual(reply["thread"], root)
        rows = self.replay()["v2_records"]
        self.assertEqual([(r["from"], r["prev"]) for r in rows], [("gemini", "none"), ("user", root)])
        self.assertEqual(rows[-1]["display_name"], "协作者")
        normal = {"action": "send", "type": "decision", "to": "gemini", "body": "A free-form decision."}
        free_form = self.post(auth, **normal)
        self.assertEqual(free_form["uuid"], free_form["thread"])
        delta = self.get_changes(auth, chunk["version"])
        self.assertEqual(len(delta["upsert"]), 2)
        self.assertEqual(delta["remove"], [])
        self.assertIn("reset", self.get_changes(auth, "old-version"))

    def test_concurrent_publish_numbers_are_unique(self):
        auth = self.login()
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(
                lambda n: self.post(auth, action="send", type="info", to="gemini", body=f"Message {n}"),
                range(8),
            ))
        self.assertEqual(sorted(reply["path"][:4] for reply in replies), [f"{n:04d}" for n in range(1, 9)])
        self.assertEqual(self.replay()["lint"]["exit_code"], 0)

    def test_decision_queues_replay_and_notifies_without_resuming(self):
        auth = self.login()
        root, filename = self.seed(needs="yes")
        original = (self.board / filename).read_bytes()
        self.assertEqual(self.get_changes(auth)["reset"]["pending"][0]["trigger_uuid"], root)
        decision = self.post(auth, action="decide", thread=root, item=root, decision="revise", comment="Add context.")
        self.post(auth, expected=409, action="decide", thread=root, item=root, decision="approve")
        self.post(auth, expected=409, action="send", type="info", thread=root, to="gemini", body="Blocked.")
        result = self.replay()
        self.assertEqual(result["lint"]["exit_code"], 0)
        self.assertEqual(result["state"]["pending"], [])
        self.assertTrue(result["state"]["threads"][root]["paused"])
        self.assertEqual(result["state"]["threads"][root]["head_uuid"], root)
        rows = [r for r in result["v2_records"] if r["uuid"] == decision["uuid"]]
        self.assertEqual((rows[0]["type"], rows[0]["prev"], rows[0]["decision"]), ("decision", root, "revise"))
        self.assertIn("Add context.", rows[0]["body"])
        item = result["state"]["inboxes"]["gemini"][0]
        self.assertTrue(web.data.board_watch._thread_allows_wake(result["state"]["threads"][root], item))
        self.assertFalse(web.data.board_watch._thread_allows_wake(result["state"]["threads"][root], {}))
        self.assertEqual((self.board / filename).read_bytes(), original)
        wake = self.post(auth, action="wake", thread=root, to="gemini")
        self.assertNotEqual(wake["thread"], root)
        follow_up = next(r for r in self.replay()["v2_records"] if r["uuid"] == wake["uuid"])
        self.assertEqual((follow_up["type"], follow_up["related_thread"], follow_up["prev"]), ("info", root, "none"))

    def test_watcher_dispatches_only_the_decision_for_a_paused_thread(self):
        watcher = web.data.board_watch
        lint.atomic_write_private_file(self.runtime / "routes.json", json.dumps({
            "version": 2,
            "routes": {"gemini": {"adapter": "gemini_agy", "enabled": True,
                                  "conversation_id": "synthetic_session"}},
            "mail": {"enabled": False},
        }).encode())
        watcher.bootstrap_runtime(self.board, self.runtime)
        root, _ = self.seed(needs="yes")
        auth = self.login()
        decision = self.post(auth, action="decide", thread=root, item=root, decision="approve")
        with patch.object(watcher.wake_routes, "wake", return_value="accepted") as wake:
            tick = watcher.run_tick(self.board, self.runtime, allow_mail=False)
        self.assertEqual(tick["wake_sent"]["uuid"], decision["uuid"])
        self.assertEqual(wake.call_args.args, ("gemini", decision["uuid"]))
        self.assertEqual(wake.call_args.kwargs["thread_id"], root)
        self.assertTrue(tick["result"]["state"]["threads"][root]["paused"])

    def test_task_create_reassign_close_pass_lint(self):
        auth = self.login()
        task = self.post(auth, action="send", type="task", to="gemini", title="Review sample", criteria="Return a summary.")
        root = task["thread"]
        old = (self.board / task["path"]).read_bytes()
        self.assertEqual(self.replay()["state"]["tasks"][root]["roles"]["assignee"], "gemini")
        reassign = self.post(auth, action="reassign", thread=root, to="codex")
        self.assertEqual(self.replay()["state"]["tasks"][root]["roles"]["assignee"], "codex")
        close = self.post(auth, action="close", thread=root)
        result = self.replay()
        self.assertEqual(result["lint"]["exit_code"], 0)
        self.assertEqual(result["state"]["tasks"][root]["state"], "done")
        self.assertTrue(result["state"]["threads"][root]["terminal"])
        self.assertEqual((self.board / task["path"]).read_bytes(), old)
        rows = {row["uuid"]: row for row in result["v2_records"]}
        self.assertIn("Return a summary.", rows[root]["body"])
        self.assertEqual(rows[reassign["uuid"]]["prev"], root)
        self.assertEqual(rows[close["uuid"]]["prev"], reassign["uuid"])
        self.assertEqual(rows[close["uuid"]]["to"], "codex")

    def test_xss_csp_and_full_text_allowlist(self):
        hostile = '</script><img src=x onerror=alert(1)>[bad](javascript:alert%281%29)\u2028'
        root, filename = self.seed(needs="yes", body=hostile)
        output = self.root / "offline.html"
        web.export_page(self.board, self.runtime, output, 30, "协作者")
        html = output.read_text()
        self.assertNotIn(hostile, html)
        self.assertIn("\\u003c/script\\u003e", html)
        self.assertIn("connect-src 'none'", html)
        self.assertNotIn("unsafe-inline", html)
        parsed = EmbeddedScripts()
        parsed.feed(html)
        self.assertEqual(set(parsed.scripts), {"inline", "board-data"})
        self.assertIn("sha256-" + web.digest(parsed.scripts["inline"]), html)
        self.assertNotRegex(parsed.scripts["inline"], r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|eval)\b")
        projected = json.loads(parsed.scripts["board-data"])["offline"]
        self.assertTrue(projected["messages"][0]["needs"])
        self.assertEqual(projected["messages"][0]["id"], root)
        self.assertIn(hostile, projected["messages"][0]["body"])
        auth = self.login()
        status, _, payload = self.request("GET", "/api/file?path=" + quote(filename), auth)
        self.assertEqual(status, 200)
        self.assertIn(hostile, json.loads(payload)["body"])
        self.assertEqual(self.request("GET", "/api/file?path=../runtime/web_url.txt", auth)[0], 404)

    def test_status_and_thread_logs_use_private_files_read_only(self):
        root, _ = self.seed(needs="yes")
        at = lint.format_utc_rfc3339(datetime.now(timezone.utc))
        pending_id = self.replay()["state"]["pending"][0]["pending_id"]
        log = (f"ts={at} route=gemini_agy agent=gemini uuid={root} status=accepted code=ok\n"
               f"ts={at} route=mail item={pending_id} status=sent code=ok\n")
        history = {
            "watch.log": log.encode(),
            "health.json": json.dumps({"as_of": at, "healthy": True}).encode(),
            "mail_ledger.json": b'{"version":2,"submissions":[{"status":"attempting"}]}',
        }
        for name, contents in history.items():
            lint.atomic_write_private_file(self.runtime / name, contents)
        view = self.get_changes(self.login())["reset"]
        self.assertEqual(view["health"]["watcher"], "ok")
        self.assertEqual([line["route"] for line in view["logs"][root]], ["gemini_agy", "mail"])
        self.assertEqual(next(a for a in view["agents"] if a["id"] == "gemini")["wake"], at)
        for name, contents in history.items():
            self.assertEqual((self.runtime / name).read_bytes(), contents)

    def test_offline_grouping_history_and_786_message_budget(self):
        body = ("汉字与普通消息 " * 850).encode()
        for number in range(1, 787):
            meta = "re: 0001\n" if number > 1 else ""
            filename = self.board / f"{number:04d}_gemini_to_user.md"
            filename.write_bytes((f"---\nfrom: gemini\nto: user\n{meta}---\n\n").encode() + body)
        import os
        stale = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
        os.utime(self.board / "0786_gemini_to_user.md", (stale, stale))
        output = self.root / "offline.html"
        start = time.perf_counter()
        size = web.export_page(self.board, self.runtime, output, None, "用户")
        elapsed = time.perf_counter() - start
        self.assertLess(size, 3_000_000)
        self.assertLess(elapsed, 5)
        parsed = EmbeddedScripts()
        parsed.feed(output.read_text())
        view = json.loads(parsed.scripts["board-data"])["offline"]
        self.assertEqual(len(view["messages"]), 786)
        self.assertEqual(len(view["threads"]), 1)
        self.assertTrue(all(row["more"] and len(row["body"].encode()) <= 4096 for row in view["messages"]))
        self.assertEqual(sum(not row["recent"] for row in view["messages"]), 1)
        self.assertEqual(len(web.data.build_view(self.replay(), self.runtime, datetime.now(timezone.utc), 30, "用户")[0]["messages"]), 785)
        print(f"\nH08 synthetic export: {size:,} bytes in {elapsed:.3f}s")
