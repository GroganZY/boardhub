"""BoardHub Web tests. Run on temporary boards with /usr/bin/python3."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
from threading import Event, Thread
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
        self.home = self.root / "home"
        self.home.mkdir()
        home = patch.dict(os.environ, {"HOME": str(self.home)})
        home.start()
        self.addCleanup(home.stop)
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

    def test_live_group_config_changes_are_published_on_poll(self):
        auth = self.login()
        before = self.get_changes(auth)
        config = self.runtime / "group.json"
        group = {"id": "test-team", "name": "测试团队"}
        lint.atomic_write_private_file(config, json.dumps(group).encode())
        configured = self.get_changes(auth, before["version"])
        self.assertEqual(configured["delta"]["group"], group)

        config.unlink()
        restored = self.get_changes(auth, configured["version"])
        self.assertEqual(restored["delta"]["group"], {"id": "default", "name": "BoardHub"})

    def test_offline_cli_group_precedence(self):
        output = self.root / "offline.html"
        config = self.runtime / "group.json"
        args = ["--board", str(self.board), "--runtime-dir", str(self.runtime), "--output", str(output)]
        flags = ["--group-id", "flag-team", "--group-name", "Flag Team"]
        flag_group = {"id": "flag-team", "name": "Flag Team"}
        cases = (
            (None, flags, flag_group),
            (b'{"id":"incomplete"}', flags, flag_group),
            (b'{"id":"invalid","name":" "}', flags, flag_group),
            (b'{"id":"file-team","name":"File Team"}', flags, {"id": "file-team", "name": "File Team"}),
            (None, flags[:2], {"id": "flag-team", "name": "BoardHub"}),
            (None, flags[2:], {"id": "default", "name": "Flag Team"}),
        )
        for content, options, expected in cases:
            with self.subTest(content=content, options=options):
                if content is None:
                    config.unlink(missing_ok=True)
                else:
                    lint.atomic_write_private_file(config, content)
                with patch("sys.stdout"):
                    self.assertEqual(web.main(args + options), 0)
                parsed = EmbeddedScripts()
                parsed.feed(output.read_text())
                self.assertEqual(json.loads(parsed.scripts["board-data"])["offline"]["group"], expected)

    def test_contract_without_session_metadata(self):
        aliases = {
            "claude": ("claude_code", "main"), "claude_main": ("claude_code", "main"),
            "claude_side": ("claude_code", "side"), "gemini": ("antigravity", None),
            "codex": ("codex", None), "gpt": ("codex", "gpt"), "dsh": ("dsh", None),
            "user": ("grogan", "web"), "grogan": ("grogan", None),
        }
        for index, sender in enumerate((*aliases, "other"), 1):
            (self.board / f"{index:04d}_{sender}_to_all.md").write_text("Legacy body.\n")
        # Malformed optional records still leave all existing view fields usable.
        lint.atomic_write_private_file(self.runtime / "registry.json", b'{"roles":[]}')
        lint.atomic_write_private_file(self.runtime / "wake_ledger.json",
                                       b'{"records":{"a":null,"b":{"agent":[]}}}')
        auth = self.login()
        before = self.get_changes(auth)
        view = before["reset"]
        self.assertEqual(view["group"], {"id": "default", "name": "BoardHub"})
        self.assertEqual([m["id"] for m in view["members"]],
                         ["claude_code", "antigravity", "codex", "dsh", "grogan"])
        self.assertTrue(all(m["sessions"] == [] for m in view["members"]))
        self.assertEqual(view["roles"]["coordinate"],
                         [{"member": "claude_code", "slot": "main"}, {"member": "claude_code", "slot": "side"}])
        self.assertEqual(view["roles"]["decide"], [{"member": "grogan"}])
        self.assertIn("agents", view)
        for message in view["messages"]:
            sender = message["from"]
            with self.subTest(sender=sender):
                if sender not in aliases:
                    self.assertNotIn("attribution", message)
                    continue
                member, label = aliases[sender]
                expected = {"member": member, "confidence": "inferred"}
                if label:
                    expected["session_label"] = label
                self.assertEqual(message["attribution"], expected)
        root, _ = self.seed()
        delta = self.get_changes(auth, before["version"])
        self.assertEqual(delta["upsert"][0]["id"], root)
        self.assertEqual(delta["upsert"][0]["attribution"], {"member": "antigravity", "confidence": "inferred"})
        self.assertNotIn("members", delta["delta"])
        old_keys = {"group", "members", "roles"}
        old_view = {key: value for key, value in view.items() if key not in old_keys}
        self.server.cache.revisions["old-view"] = old_view
        self.assertTrue(old_keys <= self.get_changes(auth, "old-view")["delta"].keys())

    def test_discovery_refresh_delivers_members_delta_after_a_source_failure(self):
        import session_discovery

        project = self.root.resolve() / "project"
        board = project / "board"
        board.mkdir(parents=True)
        runtime = self.root / "discovery-runtime"
        session_id = str(web.uuid4())
        source = self.home / ".claude" / "sessions" / "42.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({"sessionId": session_id, "name": "New session", "cwd": str(project)}))
        entered, release, failed = Event(), Event(), Event()
        scan = session_discovery.discover_sessions

        def discover(**kwargs):
            if kwargs["project_dir"] != project:
                return scan(**kwargs)
            if not entered.is_set():
                entered.set()
                release.wait(3)
                return scan(**kwargs)
            failed.set()
            raise RuntimeError("synthetic discovery failure")

        with patch.object(session_discovery, "discover_sessions", side_effect=discover), \
                patch.object(web.data, "DISCOVERY_INTERVAL", 0.02), \
                web.create_server(board, runtime, port=0) as server, \
                patch.object(self, "server", server), patch.object(self, "runtime", runtime):
            self.assertTrue(entered.wait(1))
            try:
                start = time.monotonic()
                auth = self.login()
                before = self.get_changes(auth)
                self.assertLess(time.monotonic() - start, 1)
                self.assertEqual(before["reset"]["members"][0]["sessions"], [])
            finally:
                release.set()
            self.assertTrue(failed.wait(2))
            changes = self.get_changes(auth, before["version"])
            session, = changes["delta"]["members"][0]["sessions"]
            self.assertEqual((session["id"], session["label"]), (session_id, "New session"))
            self.assertNotIn("source", session)
            self.assertEqual(changes["upsert"], [])
            self.assertFalse(self.get_changes(auth, changes["version"])["changed"])

    def test_contract_explicit_legacy_headers_are_optional(self):
        notes = {
            "0001_gemini_to_claude.md": ("member: antigravity\nsession_id: agy-1\nsession_label: G01 design\n"
                                       "model: gemini-4-carbon-medium\nsender_role: literature"),
            "0002_gemini_to_claude.md": "member: codex\nsession_id: /bad/path\nmodel: unsafe model\neffort: unknown",
            "0003_grogan_to_claude.md": "type: decision",
            "0004_dsh_to_claude.md": "type: report\nsender_role: decide",
        }
        for filename, headers in notes.items():
            (self.board / filename).write_text(f"---\n{headers}\n---\n\nLegacy body.\n")
        view = self.get_changes(self.login())["reset"]
        by_number = {row["number"]: row["attribution"] for row in view["messages"]}
        self.assertEqual(by_number["0001"], {
            "member": "antigravity", "session_id": "agy-1", "session_label": "G01 design",
            "model": "gemini-4-carbon", "effort": "medium", "role": "literature", "confidence": "explicit",
        })
        self.assertEqual(by_number["0002"], {"member": "antigravity", "confidence": "inferred"})
        self.assertEqual(by_number["0003"], {"member": "grogan", "role": "decide", "confidence": "inferred"})
        self.assertEqual(by_number["0004"], {"member": "dsh", "confidence": "inferred"})
        self.assertTrue(all(not member["sessions"] for member in view["members"]))

    def test_v2_registry_timeline_and_wake_ledger_attribution(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)

        def at(seconds):
            return lint.format_utc_rfc3339(now + timedelta(seconds=seconds))

        def write(number, sender, recipient, kind, seconds, extra=""):
            message = str(web.uuid4())
            path = self.board / f"{number:04d}_{sender}_to_{recipient}--{message}.md"
            path.write_text(
                f"---\nv: 2\nfrom: {sender}\nto: {recipient}\ntype: {kind}\nrequest_id: {message}\n"
                f"thread: {message}\nprev: none\nsent_at: {at(seconds)}\nneeds_user: no\n{extra}---\n\nBody.\n"
            )
            return message

        early = write(1, "gemini", "codex", "info", -500)
        checkin = write(2, "gemini", "all", "checkin", -400, "session_id: agy-checked-in\n")
        during = write(3, "gemini", "codex", "info", -300)
        write(4, "gemini", "all", "checkout", -200)
        after = write(5, "gemini", "codex", "info", -150)
        write(6, "gemini", "all", "checkin", -60, "session_id: agy-current\n")
        write(7, "claude_side", "all", "checkin", -90000, f"session_id: {web.uuid4()}\n")
        write(8, "claude_main", "all", "checkin", -400, f"session_id: {web.uuid4()}\n")
        registry_path = self.runtime / "registry.json"
        registry, _ = web.data.wake_routes.RoleRegistry.load(registry_path)
        registry.sync(self.replay()["registry_events"], now)

        # A registry-only check-in still applies when its source is absent locally.
        message = write(9, "claude_side", "codex", "info", -30)
        next(self.board.glob("0007_*.md")).unlink()

        thread = write(10, "codex", "user", "info", -10)
        other_thread = write(11, "codex", "user", "info", -10)
        session_id = str(web.uuid4())
        wake_path = self.runtime / "wake_ledger.json"
        trigger = str(web.uuid4())
        record = {
            "agent": "codex", "message_uuid": trigger, "status": "accepted", "last_attempt_at": at(-240),
            "spawn": {"adapter": "codex", "thread_id": thread, "session_id": session_id,
                      "model": "gpt-6.2-astra", "effort": "xhigh"},
            "attempts": [{"outcome": "accepted", "at": at(-240), "finished_at": at(-1)}],
        }
        # A later attempt on the same thread is too new to attribute its message.
        newer_trigger = str(web.uuid4())
        newer = {**record, "message_uuid": newer_trigger, "last_attempt_at": at(-8), "attempts": [],
                 "spawn": {**record["spawn"], "session_id": str(web.uuid4()), "model": "gpt-6.2-sol"}}
        lint.atomic_write_private_file(wake_path, json.dumps({
            "version": 2, "records": {f"codex:{trigger}": record, f"codex:{newer_trigger}": newer},
        }).encode())
        before = {path: path.read_bytes() for path in (registry_path, wake_path)}
        view = self.get_changes(self.login())["reset"]
        rows = {row["id"]: row["attribution"] for row in view["messages"]}
        for uuid in (early, after, other_thread):
            self.assertEqual(rows[uuid]["confidence"], "inferred")
            self.assertNotIn("session_id", rows[uuid])
        self.assertEqual(rows[checkin]["confidence"], "explicit")
        self.assertEqual(rows[during], {
            "member": "antigravity", "session_id": "agy-checked-in", "session_label": "default",
            "confidence": "registry",
        })
        self.assertEqual(rows[message]["confidence"], "registry")
        self.assertEqual(rows[thread], {
            "member": "codex", "session_id": session_id, "session_label": "#" + session_id[:8],
            "model": "gpt-6.2-astra", "effort": "xhigh", "confidence": "registry",
        })
        members = {member["id"]: member["sessions"] for member in view["members"]}
        self.assertEqual(members["antigravity"], [{
            "id": "agy-current", "label": "default", "status": "live",
            "last_seen": at(-60), "source": "registry",
        }])
        self.assertEqual([row["status"] for row in members["claude_code"]], ["idle", "stale"])
        self.assertEqual(members["codex"][0]["source"], "wake_ledger")
        self.assertEqual(members["codex"][0]["last_seen"], at(-1))
        self.assertEqual(len(members["codex"]), 2)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

        write(12, "gemini", "all", "checkin", -5, "session_id: agy-board-only\n")
        discovered = {
            "antigravity": [
                {"id": "agy-current", "label": "Discovery title", "status": "stale",
                 "last_seen": at(0), "model": "gemini-4-carbon"},
                {"id": "agy-checked-in", "status": "live", "last_seen": at(0)},
                {"id": "agy-board-only", "status": "stale", "last_seen": at(-600)},
                {"id": "agy-discovery-only", "status": "idle", "last_seen": at(0)},
                {"id": "agy-unknown-time", "status": "stale"},
            ],
            "codex": [{"id": session_id, "status": "stale", "model": "discovery-model"}],
        }
        view, _ = web.data.build_view(self.replay(), self.runtime, now, None, "user", discovered=discovered)
        members = {member["id"]: member["sessions"] for member in view["members"]}
        self.assertEqual([row["id"] for row in members["antigravity"]], [
            "agy-checked-in", "agy-discovery-only", "agy-current", "agy-board-only", "agy-unknown-time",
        ])
        agy = {row["id"]: row for row in members["antigravity"]}
        self.assertEqual(agy["agy-current"], {
            "id": "agy-current", "label": "default", "status": "live", "last_seen": at(-60),
            "source": "registry", "model": "gemini-4-carbon",
        })
        self.assertEqual(agy["agy-checked-in"]["status"], "stale")
        self.assertEqual(agy["agy-board-only"]["status"], "live")
        self.assertNotIn("source", agy["agy-discovery-only"])
        self.assertEqual(len(members["codex"]), 2)
        self.assertEqual(members["codex"][0]["status"], "live")
        self.assertEqual(members["codex"][0]["model"], "gpt-6.2-astra")

    def test_offline_grouping_history_and_786_message_budget(self):
        body = ("汉字与普通消息 " * 850).encode()
        for number in range(1, 787):
            meta = "re: 0001\n" if number > 1 else ""
            filename = self.board / f"{number:04d}_gemini_to_user.md"
            filename.write_bytes((f"---\nfrom: gemini\nto: user\n{meta}---\n\n").encode() + body)
        stale = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
        os.utime(self.board / "0786_gemini_to_user.md", (stale, stale))
        source = self.home / ".claude" / "sessions" / "42.json"
        source.parent.mkdir(parents=True)
        session_id = str(web.uuid4())
        source.write_text(json.dumps({"sessionId": session_id, "name": "Offline session"}))
        output = self.root / "offline.html"
        start = time.perf_counter()
        size = web.export_page(self.board, self.runtime, output, None, "用户")
        elapsed = time.perf_counter() - start
        self.assertLess(size, 3_000_000)
        self.assertLess(elapsed, 5)
        parsed = EmbeddedScripts()
        parsed.feed(output.read_text())
        view = json.loads(parsed.scripts["board-data"])["offline"]
        self.assertEqual(view["group"], {"id": "default", "name": "BoardHub"})
        self.assertEqual(view["members"][0]["sessions"][0]["id"], session_id)
        self.assertEqual(len(view["messages"]), 786)
        self.assertEqual(len(view["threads"]), 1)
        self.assertTrue(all(row["more"] and len(row["body"].encode()) <= 4096 for row in view["messages"]))
        self.assertEqual(sum(not row["recent"] for row in view["messages"]), 1)
        self.assertEqual(len(web.data.build_view(self.replay(), self.runtime, datetime.now(timezone.utc), 30, "用户")[0]["messages"]), 785)
        print(f"\nH08 synthetic export: {size:,} bytes in {elapsed:.3f}s")
