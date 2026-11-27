#!/usr/bin/env python3
"""Discover local session metadata for the BoardHub web member catalog."""

import argparse
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from itertools import chain
import json
import os
from pathlib import Path
import re
import socket
import stat
import time
from threading import Lock
import unicodedata


MEMBERS = ("claude_code", "antigravity", "codex", "dsh")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}")
AGY_MODEL = re.compile(r"(gemini-4-(?:carbon|argon))-(low|medium|high)")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
ROLLOUT = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-fA-F-]{36})\.jsonl")
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
CLAUDE = ("Library", "Application Support", "Claude", "claude-code-sessions")
AGY_BROKER = ("Library", "Application Support", "ClaudeAgyBroker", "agy.sock")
DSH_BROKER = ("Library", "Application Support", "ClaudeTunnelGuard", "dsh-mcp.sock")
AGY = (".gemini", "antigravity")
DOCUMENT_LIMIT = 1024 * 1024
INDEX_LIMIT = 8 * DOCUMENT_LIMIT
CODEX_HEAD = 64 * 1024
CODEX_TAIL = 32 * 1024
AGY_TAIL = 16 * 1024
CLOSED = ("archived", "closed", "stale", "run_state_archived")
SECRET = re.compile(
    r"(?i)\b(?:bearer\s+\S+|(?:[\w-]*(?:token|secret|password|api[_-]?key))\s*[:=]\s*\S+"
    r"|(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-|eyJ[a-z0-9_-]*\.)[a-z0-9._-]{8,})"
)


def _fields(names):
    return dict.fromkeys(names.split())


# Project mixed metadata/transcript JSON without decoding any unlisted values.
_TOKEN = re.compile(
    rb'[ \t\r\n]*("[^"\\]*(?:\\.[^"\\]*)*"|[{}\[\],:]|true|false|null'
    rb'|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)'
)
_SKIP = object()
_JSON_TEXT = object()
_WHITESPACE = re.compile(rb"[ \t\r\n]*")
_DEADLINE = ContextVar("discovery_deadline", default=float("inf"))


class _ScanExpired(Exception):
    pass


def _check_deadline():
    if time.monotonic() >= _DEADLINE.get():
        raise _ScanExpired


def _project(data, schema, start=0, end=None):
    end = len(data) if end is None else end
    pos = start

    def take():
        nonlocal pos
        _check_deadline()
        match = _TOKEN.match(data, pos, end)
        if match is None:
            raise ValueError
        pos = match.end()
        return match

    def kind(match):
        return data[match.start(1)]

    def value(spec, token, depth):
        if depth > 40:
            raise ValueError
        marker = kind(token)
        if marker == ord("{"):
            result = {} if isinstance(spec, dict) else None
            key = take()
            if kind(key) == ord("}"):
                return result
            while True:
                if kind(key) != ord('"') or kind(take()) != ord(":"):
                    raise ValueError
                name = json.loads(key[1]) if result is not None else None
                child_spec = spec.get(name, _SKIP) if result is not None else _SKIP
                child = value(child_spec, take(), depth + 1)
                if child_spec is not _SKIP:
                    if name in result:
                        raise ValueError
                    result[name] = child
                separator = kind(take())
                if separator == ord("}"):
                    return result
                if separator != ord(","):
                    raise ValueError
                key = take()
        if marker == ord("["):
            result = [] if isinstance(spec, list) else None
            item = take()
            if kind(item) == ord("]"):
                return result
            child_spec = spec[0] if result is not None else _SKIP
            while True:
                child = value(child_spec, item, depth + 1)
                if result is not None:
                    result.append(child)
                separator = kind(take())
                if separator == ord("]"):
                    return result
                if separator != ord(","):
                    raise ValueError
                item = take()
        if marker in b"}],:":
            raise ValueError
        if spec is _JSON_TEXT and marker == ord('"'):
            content = _WHITESPACE.match(data, token.start(1) + 1, token.end(1) - 1).end()
            if data[content] in b"{[":
                return json.loads(token[1])
        return json.loads(token[1]) if spec is None else None

    try:
        result = value(schema, take(), 0)
        if _WHITESPACE.match(data, pos, end).end() != end:
            return None
        return result
    except (ValueError, RecursionError):
        return None


@contextmanager
def _directory(parent_fd, path):
    _check_deadline()
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        info = os.fstat(fd)
        owned = info.st_uid == os.getuid()
    except OSError:
        owned = False
    try:
        yield (fd, info) if owned else (None, None)
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def _under(fd, parts):
    if fd is None or not parts:
        yield fd
        return
    with _directory(fd, parts[0]) as (child, _):
        with _under(child, parts[1:]) as directory:
            yield directory


def _names(fd):
    if fd is None:
        return
    try:
        names = sorted(os.listdir(fd))
    except OSError:
        return
    for name in names:
        _check_deadline()
        yield name


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class _FileCache:
    def __init__(self):
        self.entries = OrderedDict()

    def read(self, directory, name, reader):
        _check_deadline()
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                return None, None
            signature = _signature(info)
            key = reader, signature
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key], info.st_mtime

            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(name, flags, dir_fd=directory), "rb", buffering=0) as file:
                opened = os.fstat(file.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                        or opened.st_nlink != 1 or _signature(opened) != signature):
                    return None, None
                result = reader(file, info.st_size)
                if _signature(os.fstat(file.fileno())) != signature:
                    return None, None
        except OSError:
            return None, None

        self.entries[key] = result
        if len(self.entries) > 4096:
            self.entries.popitem(last=False)
        return result, info.st_mtime


def _timestamp(value):
    try:
        if type(value) in (int, float):
            seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
            return datetime.fromtimestamp(seconds, timezone.utc)
        if isinstance(value, str) and len(value) <= 64:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            value = re.sub(r"\.(\d+)(?=[+-])", lambda m: "." + m[1][:6].ljust(6, "0"), value)
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is not None:
                return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _latest(*values):
    return max(filter(None, map(_timestamp, values)), default=None)


def _title(value):
    if isinstance(value, str) and len(value) <= 1024 and not SECRET.search(value):
        clean = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
        clean = " ".join(clean.split())
        return clean.encode("utf-8")[:120].decode("utf-8", "ignore").rstrip() or None
    return None


def _identifier(value):
    return isinstance(value, str) and ID.fullmatch(value) and not SECRET.search(value)


def _matches_project(project, *locations):
    paths = [os.path.normpath(path) for path in locations
             if isinstance(path, str) and os.path.isabs(path) and "\0" not in path]
    return not paths or any(os.path.commonpath((project, path)) == project for path in paths)


def _session(session_id, title=None, model=None, effort=None, seen=None, closed=False):
    if not isinstance(model, str) or not MODEL.fullmatch(model) or SECRET.search(model):
        model = None
    if effort not in EFFORTS:
        effort = None
    return {"id": session_id, "label": _title(title), "model": model, "effort": effort,
            "seen": seen, "closed": closed}


def _read_source(records):
    try:
        _check_deadline()
        yield from records
    except Exception:
        # Keep usable rows and allow the other sources to finish independently.
        return


def _catalog(records, now):
    by_id = {}
    for row in _read_source(records):
        by_id.setdefault(row["id"], []).append(row)
    sessions = []
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for session_id, rows in by_id.items():
        rows.sort(key=lambda row: row["seen"] or minimum, reverse=True)
        seen = rows[0]["seen"]
        status = "stale"
        if seen and not rows[0]["closed"] and now - seen < timedelta(days=1):
            status = "live" if now - seen < timedelta(minutes=2) else "idle"
        label = next((row["label"] for row in rows if row["label"]), None)
        session = {"id": session_id, "label": label or "#" + session_id[:8], "status": status}
        for key in ("model", "effort"):
            value = next((row[key] for row in rows if row[key]), None)
            if value:
                session[key] = value
        if seen:
            session["last_seen"] = seen.isoformat().replace("+00:00", "Z")
        sessions.append((seen or minimum, session_id, session))
    sessions.sort(key=lambda item: item[1])
    sessions.sort(key=lambda item: item[0], reverse=True)
    return [item[2] for item in sessions]


_CLAUDE_FIELDS = _fields(
    "cliSessionId sessionId title name model effort cwd originCwd gitAnchorsFolderRealpath "
    "createdAt lastActivityAt lastFocusedAt startedAt updatedAt isArchived status"
)


def _claude_record(file, size):
    return _project(file.read(DOCUMENT_LIMIT + 1), _CLAUDE_FIELDS) if size <= DOCUMENT_LIMIT else None


def claude_desktop_metadata(cache, home, desktop_ids=None):
    """Yield desktop filename IDs and projected metadata. Never inspect project transcripts."""
    with _under(home, CLAUDE) as store:
        for workspace in _names(store):
            if not UUID.fullmatch(workspace):
                continue
            with _directory(store, workspace) as (workspace_fd, _):
                for org in _names(workspace_fd):
                    if not UUID.fullmatch(org):
                        continue
                    with _directory(workspace_fd, org) as (directory, _):
                        if directory is None:
                            continue
                        names = (_names(directory) if desktop_ids is None else
                                 (f"{desktop_id}.json" for desktop_id in desktop_ids))
                        for name in names:
                            if not re.fullmatch(r"local_[\w-]+\.json", name):
                                continue
                            fields, mtime = cache.read(directory, name, _claude_record)
                            if fields:
                                yield name[:-5], fields, mtime


def claude_metadata_seen(fields, mtime):
    return (_latest(fields.get("lastActivityAt"), fields.get("lastFocusedAt"), fields.get("updatedAt"))
            or _latest(fields.get("createdAt"), fields.get("startedAt")) or _timestamp(mtime))


def _claude_cli_metadata(cache, home):
    with _under(home, (".claude", "sessions")) as store:
        for name in _names(store):
            if re.fullmatch(r"(?:[0-9]+|[0-9a-fA-F-]{36})\.json", name):
                fields, mtime = cache.read(store, name, _claude_record)
                if fields:
                    yield fields.get("sessionId"), fields, mtime


def _claude(cache, home, project):
    desktop = ((fields.get("cliSessionId"), fields, mtime)
               for _, fields, mtime in claude_desktop_metadata(cache, home))
    for session_id, fields, mtime in chain(desktop, _claude_cli_metadata(cache, home)):
        if not _identifier(session_id) or not _matches_project(
            project, fields.get("cwd"), fields.get("originCwd"), fields.get("gitAnchorsFolderRealpath")
        ):
            continue
        yield _session(
            session_id, fields.get("title") or fields.get("name"), fields.get("model"),
            fields.get("effort"), claude_metadata_seen(fields, mtime),
            fields.get("isArchived") is True or _closed(fields.get("status")),
        )


_INDEX_FIELDS = _fields("id thread_name updated_at")


def _codex_index(file, size):
    titles = {}
    if size <= INDEX_LIMIT:
        for line in file.read(INDEX_LIMIT + 1).split(b"\n"):
            fields = _project(line, _INDEX_FIELDS)
            if not fields or not _identifier(fields.get("id")):
                continue
            session_id = fields["id"]
            seen = _timestamp(fields.get("updated_at"))
            previous = titles.get(session_id)
            if previous is None or previous[1] is None or (seen and seen >= previous[1]):
                titles[session_id] = _title(fields.get("thread_name")), seen
    return titles


def _tail(file, size, limit, schema, record_type):
    # Read one boundary byte to distinguish a complete first record from a fragment.
    start = max(0, size - limit)
    file.seek(max(0, start - 1))
    window = file.read(limit + 1)
    lower = 0 if start == 0 else window.find(b"\n") + 1
    if start and lower == 0:
        return {}
    end = len(window)
    while end > lower:
        if window[end - 1] == 10:
            end -= 1
        begin = window.rfind(b"\n", lower, end) + 1 or lower
        if begin < end:
            # Check the top-level type before projecting model metadata.
            envelope = _project(window, {"type": None}, begin, end)
            if envelope and envelope.get("type") == record_type:
                fields = _project(window, schema, begin, end)
                if fields:
                    return fields
        end = begin
    return {}


_CODEX_HEAD = {
    **_fields("type timestamp"),
    "payload": {**_fields("id session_id cwd timestamp title thread_name model effort"),
                "runtime_workspace_roots": [None]},
}
_CODEX_TURN = {**_fields("timestamp"), "payload": _fields("model effort")}


def _codex_header(file, size):
    head = bytearray()
    while len(head) < min(size, CODEX_HEAD):
        chunk = file.read(min(4096, CODEX_HEAD - len(head)))
        if not chunk:
            return None
        end = chunk.find(b"\n")
        if end >= 0:
            head.extend(chunk[:end])
            break
        head.extend(chunk)
    else:
        if len(head) < size:
            return None
    field = _project(head, {"type": None}) or {}
    if field.get("type") != "session_meta":
        return None
    envelope = _project(head, _CODEX_HEAD) or {}
    meta = envelope.get("payload") or {}
    meta["timestamp"] = meta.get("timestamp") or envelope.get("timestamp")
    return meta


def _codex_turn(file, size):
    return _tail(file, size, CODEX_TAIL, _CODEX_TURN, "turn_context")


def _codex(cache, home, project, now):
    with _under(home, (".codex",)) as store:
        if store is None:
            return
        index, _ = cache.read(store, "session_index.jsonl", _codex_index)
        index = index or {}
        with _directory(store, "sessions") as (sessions, _):
            today = now.astimezone().date()
            dates = {today - timedelta(days=days_ago) for days_ago in range(14)}
            dates.add(now.date())
            for date in sorted(dates, reverse=True):
                with _under(sessions, tuple(date.strftime("%Y/%m/%d").split("/"))) as day:
                    for name in _names(day):
                        match = ROLLOUT.fullmatch(name)
                        if not match:
                            continue
                        meta, mtime = cache.read(day, name, _codex_header)
                        if not meta:
                            continue
                        session_id = meta.get("id") or meta.get("session_id")
                        locations = meta.get("runtime_workspace_roots") or []
                        if (session_id != match[1] or not UUID.fullmatch(session_id)
                                or not _matches_project(project, meta.get("cwd"), *locations)):
                            continue
                        turn, _ = cache.read(day, name, _codex_turn)
                        turn = turn or {}
                        label, updated = index.get(session_id, (None, None))
                        last_turn = _timestamp(turn.get("timestamp")) or _timestamp(mtime)
                        seen = max(filter(None, (last_turn, updated)), default=_timestamp(meta.get("timestamp")))
                        payload = turn.get("payload") or {}
                        yield _session(
                            session_id, label or meta.get("title") or meta.get("thread_name"),
                            payload.get("model") or meta.get("model"),
                            payload.get("effort") or meta.get("effort"), seen,
                        )


def _socket_signature(home, parts):
    with _under(home, parts[:-1]) as parent:
        if parent is None:
            return None
        try:
            directory = os.fstat(parent)
            info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if not directory.st_mode & 0o077 and stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                return _signature(info)
        except OSError:
            pass
    return None


def _exchange(path, request, deadline):
    wire = json.dumps(request, separators=(",", ":")).encode("ascii") + b"\n"
    reply = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            def set_timeout():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.settimeout(remaining)

            set_timeout()
            connection.connect(str(path))
            set_timeout()
            connection.sendall(wire)
            while len(reply) <= DOCUMENT_LIMIT:
                set_timeout()
                chunk = connection.recv(min(65536, DOCUMENT_LIMIT + 1 - len(reply)))
                if not chunk:
                    break
                reply.extend(chunk)
                if b"\n" in chunk:
                    if len(reply) <= DOCUMENT_LIMIT and reply.endswith(b"\n") and reply.count(b"\n") == 1:
                        return reply
                    break
    except OSError:
        pass
    return b""


def _closed(state):
    return isinstance(state, str) and state.lower() in CLOSED


_AGY_FIELDS = _fields(
    "title model effort archived isArchived runState status createTime createdAt updateTime updatedAt"
)
_AGY_META = {**_AGY_FIELDS, "metadata": _AGY_FIELDS, "annotations": _AGY_FIELDS, "state": _AGY_FIELDS}
_AGY_REPLY = {"ok": None, "result": {"response": {"conversationMetadata": _AGY_META}}}
_AGY_LOG = _fields("model created_at")
_PBTXT_TITLE = re.compile(rb'(?m)^title[ \t]*:[ \t]*("(?:[^"\\]|\\.)*")[ \t\r]*$')


def _agy_title(file, size):
    if size > DOCUMENT_LIMIT:
        return None
    match = _PBTXT_TITLE.search(file.read(DOCUMENT_LIMIT + 1))
    return _project(match[1], None) if match else None


def _agy_log(file, size):
    return _tail(file, size, AGY_TAIL, _AGY_LOG, "PLANNER_RESPONSE")


def _agy_metadata(path, session_id, deadline):
    reply = _exchange(path, {"op": "get_conversation_metadata", "args": {"conversation_id": session_id}}, deadline)
    payload = _project(reply, _AGY_REPLY) or {}
    response = (payload.get("result") or {}).get("response") or {}
    conversation = response.get("conversationMetadata") or {}
    if payload.get("ok") is not True:
        return None
    return {
        **{key: conversation[key] for key in _AGY_FIELDS if key in conversation},
        **(conversation.get("metadata") or {}), **(conversation.get("annotations") or {}),
        **(conversation.get("state") or {}),
    }


def _agy(cache, home, now, broker):
    with _under(home, AGY) as store:
        if store is None:
            return
        with _directory(store, "brain") as (brain, _), _directory(store, "annotations") as (annotations, _):
            if brain is None:
                return
            candidates = []
            for session_id in _names(brain):
                if UUID.fullmatch(session_id):
                    with _directory(brain, session_id) as (_, info):
                        if info and now - _timestamp(info.st_mtime) < timedelta(days=30):
                            candidates.append((info.st_mtime, session_id))
            candidates.sort(reverse=True)
            for mtime, session_id in candidates:
                fields = broker(session_id) or {}
                title, model = fields.get("title"), fields.get("model")
                log = {}
                if not title and annotations is not None:
                    title, _ = cache.read(annotations, session_id + ".pbtxt", _agy_title)
                if not model:
                    with _under(brain, (session_id, ".system_generated", "logs")) as logs:
                        if logs is not None:
                            log, _ = cache.read(logs, "transcript.jsonl", _agy_log)
                            log = log or {}
                            model = log.get("model")
                bundled = AGY_MODEL.fullmatch(model) if isinstance(model, str) else None
                effort = fields.get("effort")
                if bundled:
                    model = bundled[1]
                    if effort not in EFFORTS:
                        effort = bundled[2]
                seen = (_latest(fields.get("updateTime"), fields.get("updatedAt"), log.get("created_at"))
                        or _latest(fields.get("createTime"), fields.get("createdAt")) or _timestamp(mtime))
                yield _session(session_id, title, model, effort, seen,
                               fields.get("archived") is True or fields.get("isArchived") is True
                               or _closed(fields.get("runState") or fields.get("status")))


_DSH_ROW = _fields("sessionId id cwd title name model effort status state createdAt updatedAt")
_DSH_INDEX = {"sessions": [_DSH_ROW]}
_DSH_RESULT = {"isError": None, "structuredContent": _DSH_INDEX}
_DSH_RPC = {**_fields("id jsonrpc"), "error": {}, "result": _DSH_RESULT}


def _dsh_rows(path, deadline):
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "dsh_list_sessions", "arguments": {}},
    }
    reply = _exchange(path, request, deadline)
    payload = _project(reply, {**_DSH_RPC, "ok": None, "response": _DSH_RPC}) or {}
    rpc = payload.get("response") if payload.get("ok") is True else payload
    if not rpc or rpc.get("jsonrpc") != "2.0" or type(rpc.get("id")) is not int or rpc["id"] != 1 or "error" in rpc:
        return None
    result = rpc.get("result") or {}
    if result.get("isError", False) is not False:
        return None
    structured = result.get("structuredContent") or {}
    if isinstance(structured.get("sessions"), list):
        return structured["sessions"]

    # Some MCP servers serialize their list index as a JSON text block.
    text_result = {"result": {"content": [{"type": None, "text": _JSON_TEXT}]}}
    schema = {"response": text_result} if payload.get("ok") is True else text_result
    blocks = _project(reply, schema) or {}
    blocks = (blocks.get("response") or {}) if payload.get("ok") is True else blocks
    for item in (blocks.get("result") or {}).get("content") or []:
        if item and item.get("type") == "text" and isinstance(item.get("text"), str):
            data = item["text"].encode("utf-8", "replace")
            index = _project(data, _DSH_INDEX) or {}
            if isinstance(index.get("sessions"), list):
                return index["sessions"]
            rows = _project(data, [_DSH_ROW])
            if isinstance(rows, list):
                return rows
    return None


def _dsh(rows, project):
    for row in rows or ():
        _check_deadline()
        if not row:
            continue
        session_id = row.get("sessionId") or row.get("id")
        if _identifier(session_id) and _matches_project(project, row.get("cwd")):
            seen = _timestamp(row.get("updatedAt")) or _timestamp(row.get("createdAt"))
            yield _session(session_id, row.get("title") or row.get("name"), row.get("model"),
                           row.get("effort"), seen, _closed(row.get("status") or row.get("state")))


class SessionDiscovery:
    """Reuse one instance to cache projected file metadata across scans."""

    def __init__(self):
        self._files = _FileCache()
        self._broker_cache = OrderedDict()
        self._lock = Lock()

    def _broker(self, key, ttl, deadline, query):
        now = time.monotonic()
        cached = self._broker_cache.get(key)
        if cached and now < cached[0]:
            self._broker_cache.move_to_end(key)
            return cached[1]
        if now >= deadline:
            return None
        result = query()
        if result is None and time.monotonic() >= deadline:
            return None
        self._broker_cache[key] = (now + ttl, result)
        self._broker_cache.move_to_end(key)
        if len(self._broker_cache) > 1024:
            self._broker_cache.popitem(last=False)
        return result

    def discover(self, home=None, project_dir=None, *, timeout=None):
        deadline = float("inf") if timeout is None else time.monotonic() + timeout
        home = Path(home if home is not None else Path.home()).resolve()
        if project_dir is None:
            source = Path(__file__).resolve()
            project_dir = source.parents[2] if source.parent.name == "hub" and source.parents[1].name == "bridge" else Path.cwd()
        project = os.path.abspath(project_dir)
        now = datetime.now(timezone.utc)
        wait = -1 if timeout is None else max(0, deadline - time.monotonic())
        if not self._lock.acquire(timeout=wait):
            raise TimeoutError("Session discovery is busy")
        try:
            with _directory(None, home) as (home_fd, _):
                if home_fd is None:
                    return {member: [] for member in MEMBERS}
                return self._scan(home, home_fd, project, now, deadline)
        finally:
            self._lock.release()

    def _scan(self, home, home_fd, project, now, deadline):
        def agy():
            signature = _socket_signature(home_fd, AGY_BROKER)
            start = time.monotonic()
            # Leave half the source's remaining budget for on-disk fallbacks.
            broker_deadline = start + min(2.5, (_DEADLINE.get() - start) / 2)

            def metadata(session_id):
                if signature is None:
                    return None
                return self._broker(
                    (home, signature, session_id), 60, broker_deadline,
                    lambda: _agy_metadata(home.joinpath(*AGY_BROKER), session_id, broker_deadline),
                )

            yield from _agy(self._files, home_fd, now, metadata)

        def dsh():
            signature = _socket_signature(home_fd, DSH_BROKER)
            if signature is not None:
                broker_deadline = min(time.monotonic() + 2.5, _DEADLINE.get())
                rows = self._broker(
                    (home, signature), 90, broker_deadline,
                    lambda: _dsh_rows(home.joinpath(*DSH_BROKER), broker_deadline),
                )
                yield from _dsh(rows, project)

        sources = (_claude(self._files, home_fd, project), agy(), _codex(self._files, home_fd, project, now), dsh())
        catalog = {}
        for i, (member, records) in enumerate(zip(MEMBERS, sources)):
            start = time.monotonic()
            # Share the remaining time so a slow source leaves time for the rest.
            token = _DEADLINE.set(start + (deadline - start) / (len(sources) - i))
            try:
                catalog[member] = _catalog(records, now)
            finally:
                _DEADLINE.reset(token)
        return catalog


_DISCOVERY = SessionDiscovery()


def discover_sessions(*, home=None, project_dir=None, timeout=None):
    """Return a mapping from member ID to C01 sessions[] rows."""
    return _DISCOVERY.discover(home=home, project_dir=project_dir, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--json", action="store_true", required=True, help="print metadata as JSON")
    parser.add_argument("--project-dir", type=Path, help="override the project directory used for scoping")
    args = parser.parse_args()
    print(json.dumps(discover_sessions(project_dir=args.project_dir), allow_nan=False))


if __name__ == "__main__":
    main()
