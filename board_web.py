#!/usr/bin/env python3
"""BoardHub's loopback-only web server and offline HTML exporter (Python 3.9+)."""

import argparse
import base64
from datetime import datetime, timezone
import errno
import hashlib
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
from threading import RLock, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


def load_lint():
    if "board_lint" in sys.modules:
        return sys.modules["board_lint"]
    path = Path(__file__).resolve().with_name("board_lint.py")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RuntimeError("Unsafe sibling board_lint.py")
    finally:
        os.close(fd)
    spec = importlib.util.spec_from_file_location("board_lint", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["board_lint"] = module
    spec.loader.exec_module(module)
    return module


board_lint = load_lint()
data = board_lint.load_sibling_module("board_web_data")
board_state = data.board_state

COOKIE = "boardhub_web"
CSRF_HEADER = "X-BoardHub-CSRF"
MAX_REQUEST = 72 * 1024
OUTCOMES = {"approve": "同意", "reject": "拒绝", "revise": "需要修改"}


def script_json(value):
    content = data.json_bytes(value).decode("utf-8")
    for char in "<>&\u2028\u2029":
        content = content.replace(char, "\\u%04x" % ord(char))
    return content


def digest(content):
    return base64.b64encode(hashlib.sha256(content.encode("utf-8")).digest()).decode("ascii")


class Page:
    def __init__(self):
        here = Path(__file__).resolve().parent
        self.html = (here / "board_web.html").read_text(encoding="utf-8")
        self.css = (here / "board_web.css").read_text(encoding="utf-8")
        self.js = (here / "board_web.js").read_text(encoding="utf-8")

    def render(self, initial, offline=False):
        connect = "'none'" if offline else "'self'"
        csp = (
            f"default-src 'none'; script-src 'sha256-{digest(self.js)}'; "
            f"style-src 'sha256-{digest(self.css)}'; "
            f"connect-src {connect}; "
            "base-uri 'none'; form-action 'none'; object-src 'none'"
        )
        # Insert the inert data block last so board text cannot be interpreted as template markers.
        markup = self.html.replace("{{CSP}}", csp).replace("{{CSS}}", self.css).replace("{{JS}}", self.js)
        markup = markup.replace("{{DATA}}", script_json(initial))
        return markup.encode("utf-8"), csp + "; frame-ancestors 'none'"


def export_page(board, runtime, output, since_days, display_name, *, include_mainline=False, group_fallback=None):
    output = Path(output)
    if output.parent.resolve().is_relative_to(Path(board).resolve()):
        raise ValueError("Choose an export path outside the board.")
    now = datetime.now(timezone.utc)
    result = data.read_board(board, runtime, now)
    if not result["health"]["healthy"] or result["health"]["runtime_corrupt"]:
        raise ValueError("Board scan unavailable.")
    discovered = data.read_discovered_sessions(board)
    dispatches = [] if include_mainline else None
    view, bodies = data.build_view(result, runtime, now, since_days, display_name,
                                  discovered=discovered, dispatches=dispatches, group_fallback=group_fallback)
    if include_mainline:
        references = data.claude_transcript.BoardReferences(view["messages"], dispatches)
        view["mainline"] = data.read_mainline(data.new_mainline_reader(), runtime, references, now, since_days)
    view["board_url"] = Path(board).resolve().as_uri()
    page = Page()
    html, _ = page.render({"offline": view}, offline=True)
    # Leave room for metadata and escaped JSON even on boards full of long messages.
    limit = data.BODY_LIMIT
    while len(html) >= data.PAGE_LIMIT and limit:
        limit = max(0, min(limit - 1, int(limit * (data.PAGE_LIMIT / len(html)) * 0.84)))
        for row in view["messages"]:
            row["body"], row["more"] = data._preview(bodies[row["path"]], limit)
        html, _ = page.render({"offline": view}, offline=True)
    if include_mainline:
        items = view["mainline"]["items"]
        while len(html) >= data.PAGE_LIMIT and items:
            del items[:max(1, len(items) // 8)]
            html, _ = page.render({"offline": view}, offline=True)
    board_lint.atomic_write_private_file(output, html)
    return len(html)


class ActionError(ValueError):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def field(request, name, limit, required=True, multiline=False):
    value = request.get(name, "")
    if multiline and isinstance(value, str):
        value = value.replace("\r\n", "\n")
    if not isinstance(value, str) or board_lint.has_disallowed_control_chars(value, allow_newlines=multiline):
        raise ActionError(f"字段 {name} 包含无效字符。")
    value = value.strip()
    try:
        valid_size = len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        valid_size = False
    if not valid_size or (required and not value):
        raise ActionError(f"请检查字段 {name} 的内容和长度。")
    return value


def recipient(request, allow_all=True):
    actor = field(request, "to", 32)
    if actor not in board_lint.AGENT_PARTICIPANTS and not (allow_all and actor == "all"):
        raise ActionError("请选择收件人。")
    return actor


def publish_action(cache, request):
    action = field(request, "action", 12)
    target = field(request, "thread", 36, required=False)
    cache.refresh(force=True)
    result = cache.result
    if not result["health"]["healthy"] or result["health"]["runtime_corrupt"]:
        raise ActionError("看板暂时不可写，请稍后重试。", HTTPStatus.SERVICE_UNAVAILABLE)
    state = result["state"]
    thread = state["threads"].get(target)
    if target and (not thread or not thread["head_uuid"]):
        raise ActionError("找不到可回复的会话。", HTTPStatus.CONFLICT)
    task = state["tasks"].get(target)
    locked = thread and (thread["paused"] or thread["frozen"] or thread["terminal"])
    msg_id = str(uuid4())
    parent = thread["head_uuid"] if thread else "none"
    extra = {"display_name": cache.display_name}

    if action == "decide":
        trigger = field(request, "item", 36)
        pending = next((item for item in state["pending"]
                        if item["trigger_uuid"] == trigger and item["thread_id"] == target
                        and item["reason"] == "needs_user"), None)
        if not pending or any(reason != "needs_user" for reason in thread["freeze_reasons"]):
            raise ActionError("此请求已处理或暂时不可用。", HTTPStatus.CONFLICT)
        row = next(row for row in result["v2_records"]
                   if row["uuid"] == trigger and row["status"] == "accepted")
        actor = row["from"] if row["from"] in board_lint.AGENT_PARTICIPANTS else row["to"]
        to = actor if actor in board_lint.AGENT_PARTICIPANTS else "all"
        choice = field(request, "decision", 8)
        if choice not in OUTCOMES:
            raise ActionError("请选择一个决定。")
        comment = field(request, "comment", 48 * 1024, required=False, multiline=True)
        kind, parent = "decision", trigger
        extra["decision"] = choice
        body = OUTCOMES[choice] + ("\n\n" + comment if comment else "")
    elif action == "wake":
        if thread is None:
            raise ActionError("请选择会话。")
        to = recipient(request, allow_all=False)
        kind, body = "info", f"请查看会话 `{target}` 的最新进展。"
        if locked:
            extra["related_thread"] = target
            target, parent = "", "none"
    elif action in ("reassign", "close"):
        if not task or locked:
            raise ActionError("此任务当前不可更改。", HTTPStatus.CONFLICT)
        previous = task["roles"]["assignee"]
        kind, extra["action"] = "task_update", action
        if action == "reassign":
            to = recipient(request, allow_all=False)
            reviewer = task["roles"]["reviewer"]
            if to in (previous, reviewer) or (to == "dsh" and task["kind"] == "code" and reviewer != "codex"):
                raise ActionError("请选择符合此任务复核规则的新负责人。")
            extra["assignee"] = to
            body = f"负责人由 {previous} 转交给 {to}。"
        else:
            to, body = previous, "任务已关闭。"
    elif action == "send":
        kind = field(request, "type", 12)
        if kind == "task":
            to = recipient(request, allow_all=False)
            title = field(request, "title", board_lint.MAX_TASK_LABEL_BYTES)
            criteria = field(request, "criteria", 12 * 1024, multiline=True)
            note = field(request, "body", 46 * 1024, required=False, multiline=True)
            body = (note + "\n\n" if note else "") + "## 完成标准\n\n" + criteria
            extra.update(task=title, assignee=to, reviewer="none", kind="coordination")
            target, parent = "", "none"
        elif kind in ("info", "question", "decision"):
            if locked:
                raise ActionError("此会话已暂停或关闭。", HTTPStatus.CONFLICT)
            to = recipient(request)
            body = field(request, "body", 60 * 1024, multiline=True)
        else:
            raise ActionError("请选择一种消息类型。")
    else:
        raise ActionError("未知的操作。")

    headers = {
        "v": 2, "from": board_lint.WEB_USER, "to": to, "type": kind,
        "request_id": msg_id, "thread": target or msg_id, "prev": parent,
        "sent_at": board_lint.format_utc_rfc3339(datetime.now(timezone.utc)),
        "needs_user": "no", **extra,
    }
    raw = ("---\n" + "".join(f"{key}: {value}\n" for key, value in headers.items())
           + "---\n\n" + body + "\n").encode("utf-8")
    try:
        published = board_state.publish_message(cache.board, None, board_lint.WEB_USER, to, msg_id, raw)
    except board_state.DurabilityError as exc:
        raise ActionError("看板写入失败，请检查后重试。", HTTPStatus.SERVICE_UNAVAILABLE) from exc
    except board_state.PublishError as exc:
        raise ActionError(str(exc), HTTPStatus.CONFLICT) from exc
    cache.signature = None
    return {"uuid": published["uuid"], "thread": target or msg_id, "path": published["filename"]}


def matches(value, token):
    return isinstance(value, str) and value.isascii() and secrets.compare_digest(value, token)


class WebHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, _format, *_args):
        # Request targets may contain the bootstrap token.
        pass

    def reply(self, status, payload=b"", kind="text/plain; charset=utf-8", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def json_reply(self, status, payload):
        self.reply(status, data.json_bytes(payload), "application/json; charset=utf-8")

    def target(self):
        hosts = self.headers.get_all("Host") or []
        port = self.server.server_port
        allowed = {"127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"}
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            self.reply(HTTPStatus.FORBIDDEN, b"Invalid Host")
            return None
        if not self.path.startswith("/") or self.path.startswith("//"):
            self.reply(HTTPStatus.BAD_REQUEST)
            return None
        return urlsplit(self.path)

    def has_cookie(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        value = cookies.get(COOKIE)
        return value is not None and matches(value.value, self.server.session)

    def authorize_api(self):
        if not self.has_cookie():
            self.json_reply(HTTPStatus.UNAUTHORIZED, {"error": "请使用本机启动链接登录。"})
            return False
        csrf = self.headers.get_all(CSRF_HEADER) or []
        if len(csrf) != 1 or not matches(csrf[0], self.server.csrf):
            self.json_reply(HTTPStatus.FORBIDDEN, {"error": "请求校验失败，请刷新页面。"})
            return False
        return True

    def do_GET(self):
        target = self.target()
        if target is None:
            return
        try:
            query = parse_qs(target.query, max_num_fields=8)
        except ValueError:
            self.reply(HTTPStatus.BAD_REQUEST)
            return
        if target.path == "/":
            tokens = query.get("t", [])
            if len(tokens) == 1 and matches(tokens[0], self.server.token):
                self.reply(HTTPStatus.SEE_OTHER, headers=(
                    ("Location", "/"),
                    ("Set-Cookie", f"{COOKIE}={self.server.session}; Path=/; HttpOnly; SameSite=Strict"),
                ))
            elif self.has_cookie():
                page, csp = self.server.index
                self.reply(HTTPStatus.OK, page, "text/html; charset=utf-8", (("Content-Security-Policy", csp),))
            else:
                self.reply(HTTPStatus.UNAUTHORIZED, "请打开 web_url.txt 中的本机链接。".encode("utf-8"))
            return

        if not target.path.startswith("/api/"):
            self.reply(HTTPStatus.NOT_FOUND)
            return
        if not self.authorize_api():
            return
        with self.server.lock:
            try:
                if target.path == "/api/changes":
                    payload = self.server.cache.changes(query.get("since", [""])[0])
                elif target.path == "/api/file":
                    self.server.cache.refresh()
                    name = query.get("path", [""])[0]
                    if name not in self.server.cache.full_text:
                        self.json_reply(HTTPStatus.NOT_FOUND, {"error": "找不到此留言。"})
                        return
                    payload = {"body": self.server.cache.full_text[name]}
                else:
                    self.reply(HTTPStatus.NOT_FOUND)
                    return
            except OSError:
                self.json_reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "暂时无法读取看板。"})
                return
        self.json_reply(HTTPStatus.OK, payload)

    def do_POST(self):
        target = self.target()
        if target is None:
            return
        if not target.path.startswith("/api/"):
            self.reply(HTTPStatus.NOT_FOUND)
            return
        if not self.authorize_api():
            return
        if target.path != "/api/message":
            self.reply(HTTPStatus.NOT_FOUND)
            return
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0]):
            self.reply(HTTPStatus.BAD_REQUEST)
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_REQUEST:
            self.reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        if self.headers.get_content_type() != "application/json":
            self.reply(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            raw = self.rfile.read(length)
            request = board_lint.decode_json_strict(raw, max_bytes=MAX_REQUEST, max_depth=3)
            if not isinstance(request, dict):
                raise ActionError("消息请求必须是 JSON 对象。")
            with self.server.lock:
                result = publish_action(self.server.cache, request)
        except board_lint.FormatError:
            self.json_reply(HTTPStatus.BAD_REQUEST, {"error": "无效的消息格式。"})
        except ActionError as exc:
            self.json_reply(exc.status, {"error": str(exc)})
        except OSError:
            self.json_reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "暂时无法写入看板。"})
        else:
            self.json_reply(HTTPStatus.CREATED, result)


class WebServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, port, board, runtime, since_days, display_name, *, group_fallback=None):
        self.token, self.session, self.csrf = (secrets.token_urlsafe(32) for _ in range(3))
        self.index = Page().render({"csrf": self.csrf})
        self.cache = data.BoardView(board, runtime, since_days, display_name, group_fallback=group_fallback)
        self.lock = RLock()
        super().__init__(("127.0.0.1", port), WebHandler)

    def server_close(self):
        self.cache.discovery_stop.set()
        super().server_close()


def create_server(board, runtime, port=8765, since_days=30, display_name="user", *, group_fallback=None):
    display_name = field({"name": display_name}, "name", 120)
    runtime = board_lint.verify_runtime_dir(Path(runtime), create=True)
    for candidate in (range(port, 65536) if port else (0,)):
        try:
            server = WebServer(candidate, board, runtime, since_days, display_name, group_fallback=group_fallback)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
        else:
            url = f"http://127.0.0.1:{server.server_port}/?t={server.token}"
            try:
                board_lint.atomic_write_private_file(runtime / "web_url.txt", (url + "\n").encode("ascii"))
                server.cache.start_discovery()
            except BaseException:
                server.server_close()
                raise
            return server
    raise OSError(errno.EADDRINUSE, "No free BoardHub Web port.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    if hasattr(board_lint, "resolve_cli_settings"):
        parser.add_argument("--config", type=Path)
        parser.add_argument("--human")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--name", default="user", help="Display name for messages from user.")
    parser.add_argument("--group-id", help="Group ID if runtime group.json is unavailable.")
    parser.add_argument("--group-name", help="Group name if runtime group.json is unavailable.")
    history = parser.add_mutually_exclusive_group()
    history.add_argument("--since-days", type=int, default=30)
    history.add_argument("--all", action="store_true")
    parser.add_argument("--output", type=Path, help="Export a read-only offline HTML file.")
    parser.add_argument("--include-mainline", action="store_true",
                        help="Include the private mainline in an offline export.")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or not 1 <= args.since_days <= 36500:
        parser.error("port must be 0–65535 and since-days must be 1–36500")
    if args.include_mainline and not args.output:
        parser.error("--include-mainline requires --output")
    since_days = None if args.all else args.since_days
    group_fallback = {"id": args.group_id, "name": args.group_name}
    try:
        name = field({"name": args.name}, "name", 120)
        if hasattr(board_lint, "resolve_cli_settings"):
            board, runtime, _ = board_lint.resolve_cli_settings(args)
        else:
            board = args.board or data.board_watch.DEFAULT_BOARD_DIR
            runtime = args.runtime_dir or data.board_watch.DEFAULT_RUNTIME_DIR
        if args.output:
            size = export_page(board, runtime, args.output, since_days, name,
                               include_mainline=args.include_mainline, group_fallback=group_fallback)
            print(f"Generated {args.output} ({size:,} bytes)")
            return 0
        with create_server(board, runtime, args.port, since_days, name, group_fallback=group_fallback) as server:
            print(f"BoardHub Web is listening on 127.0.0.1:{server.server_port}. Open {runtime / 'web_url.txt'}.", flush=True)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda _sig, _frame: Thread(target=server.shutdown, daemon=True).start())
            server.serve_forever()
    except (OSError, ValueError, board_lint.SecurityError, board_lint.FormatError) as exc:
        print(f"BoardHub Web: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
