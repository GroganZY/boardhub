"""Read-only BoardHub Web projections shared by the server and offline export."""

from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import time

import board_lint

board_state = board_lint.load_sibling_module("board_state")
board_watch = board_lint.load_sibling_module("board_watch")
wake_routes = board_lint.load_sibling_module("wake_routes")

BODY_LIMIT = 4096
PAGE_LIMIT = 3_000_000
RUNTIME_FILES = (
    "manifest.json", "baseline.json", "housekeeping.json", "health.json",
    "registry.json", "routes.json", "watch.log", "wake_ledger.json", "mail_pending.json",
)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="replace")


def private_json(path):
    try:
        return board_lint.read_private_json_file(path) or {}
    except (board_lint.SecurityError, board_lint.FormatError, OSError):
        return {}


def read_board(board, runtime, now):
    manifest, manifest_bad = board_state.load_manifest(runtime / "manifest.json")
    dismissed, _, receipts_bad = board_state.load_housekeeping_receipts(runtime / "housekeeping.json")
    baseline, baseline_bad = board_watch._load_baseline(runtime / "baseline.json")
    snapshot = board_lint.scan_board(board)
    if snapshot.healthy and manifest and isinstance(manifest.get("board_id"), str):
        if not board_watch._verify_board_root_pinned(
            board, snapshot.root_dev, snapshot.root_ino, expected_board_id=manifest["board_id"]
        ):
            snapshot = board_lint.BoardSnapshot(
                healthy=False, board_dir=str(board), scan_error="board_identity_changed"
            )
    return board_state.rebuild(
        snapshot, manifest or {}, now, housekeeping_ids=dismissed, baseline_uuids=baseline,
        runtime_corrupt=manifest_bad or receipts_bad or baseline_bad,
    )


def inventory(board, runtime):
    """Cheap invalidation only. All data comes from the watcher's safe two-pass scan."""
    def stamp(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def path_stamp(path):
        try:
            return stamp(path.lstat())
        except OSError:
            return None

    try:
        with os.scandir(board) as directory:
            entries = sorted(
                (entry.name, stamp(entry.stat(follow_symlinks=False)))
                for entry in directory if not board_lint._is_ignored_entry(entry.name)
            )
    except OSError:
        entries = None
    return path_stamp(board / board_watch.BOARD_ID_NAME), path_stamp(board), entries, tuple(
        path_stamp(runtime / name) for name in RUNTIME_FILES
    )


def watch_log(runtime):
    try:
        fd = board_lint.verify_regular_file(runtime / "watch.log")
    except (board_lint.SecurityError, OSError):
        return []
    try:
        with os.fdopen(fd, "rb") as stream:
            stream.seek(max(0, os.fstat(fd).st_size - 256 * 1024))
            lines = stream.read(256 * 1024).decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    return [dict(word.split("=", 1) for word in line.split() if "=" in word) for line in lines]


def thread_logs(result, runtime):
    message_threads = {
        row["uuid"]: row.get("related_thread") or row["thread"]
        for row in result["v2_records"]
    }
    state = result["state"]
    item_threads = {
        item["pending_id"]: item["thread_id"]
        for key in ("pending", "resolved_pending") for item in state.get(key, [])
    }
    for digest in (result.get("mail_pending") or {}, private_json(runtime / "mail_pending.json")):
        items = digest.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("item_id"), str):
                thread = item.get("thread_id")
                if isinstance(thread, str):
                    item_threads[item["item_id"]] = thread

    logs = defaultdict(list)
    latest = {}
    for row in watch_log(runtime):
        at = row.get("ts")
        status = row.get("status", "")
        route = row.get("route", "")
        agent = row.get("agent", "")
        if not board_lint.parse_utc_rfc3339(at) or not wake_routes.SAFE_CODE_RE.fullmatch(status):
            continue
        if route == "mail":
            thread = item_threads.get(row.get("item")) or row.get("thread")
        elif agent in board_lint.AGENT_PARTICIPANTS and wake_routes.SAFE_CODE_RE.fullmatch(route):
            thread = message_threads.get(row.get("uuid"))
            if status == "accepted" and at > latest.get(agent, ""):
                latest[agent] = at
        else:
            continue
        if thread not in state["threads"]:
            continue
        entry = {"at": at, "route": route, "status": status}
        if agent in board_lint.AGENT_PARTICIPANTS:
            entry["agent"] = agent
        code = row.get("code", "")
        if wake_routes.SAFE_CODE_RE.fullmatch(code):
            entry["code"] = code
        logs[thread].append(entry)
    return {thread: lines[-12:] for thread, lines in logs.items()}, latest


def presence(runtime, result, now, latest, display_name):
    routes, routes_bad = wake_routes.load_route_config(runtime / "routes.json")
    registry, registry_bad = wake_routes.RoleRegistry.load(runtime / "registry.json")
    wakes = private_json(runtime / "wake_ledger.json").get("records", {})
    if isinstance(wakes, dict):
        for record in wakes.values():
            if not isinstance(record, dict) or record.get("agent") not in board_lint.AGENT_PARTICIPANTS:
                continue
            agent = record["agent"]
            attempts = record.get("attempts")
            if isinstance(attempts, list):
                for attempt in attempts:
                    if isinstance(attempt, dict) and attempt.get("outcome") == "accepted":
                        at = attempt.get("at")
                        if board_lint.parse_utc_rfc3339(at) and at > latest.get(agent, ""):
                            latest[agent] = at

    primary = ("claude_main", "claude_side", "codex", "gemini", "dsh")
    agents = []
    for agent in primary + tuple(sorted(board_lint.AGENT_PARTICIPANTS - set(primary))):
        route = routes["routes"][agent]
        if routes_bad or registry_bad:
            status = "unavailable"
        elif not route["enabled"]:
            status = "inbox_only" if route["adapter"] == "inbox" else "disabled"
        else:
            _, status = registry.resolve(agent, route, now, routes["stale_days"])
        record = registry.roles.get(agent, {})
        wake_at = max(latest.get(agent, ""), record.get("last_successful_wake_at") or "") or None
        recent_event = result.get("registry_events", {}).get(agent, {})
        session = recent_event.get("session_id") if recent_event else (
            record.get("session_id") if record.get("confirmed") else None
        )
        agents.append({"id": agent, "route": status, "session": session, "wake": wake_at})
    agents.append({"id": board_lint.WEB_USER, "name": display_name, "route": "local"})
    raw = private_json(runtime / "health.json")
    last_scan = board_lint.parse_utc_rfc3339(raw.get("as_of"))
    healthy = bool(last_scan and now - last_scan < timedelta(seconds=90)
                   and raw.get("healthy") is True and raw.get("stale") is not True)
    return agents, {
        "watcher": "ok" if healthy else ("stale" if last_scan else "unknown"),
        "scan": raw.get("as_of") if last_scan else None,
        "board": result["health"]["healthy"] and not result["health"]["runtime_corrupt"],
        "routes_ok": sum(agent["route"] == "ok" for agent in agents),
        "mail": None if routes_bad else bool(routes["mail"]["enabled"]),
    }


def _preview(body, cap):
    raw = body.encode("utf-8", errors="replace")
    if len(raw) <= cap:
        return body, False
    return raw[:cap].decode("utf-8", "ignore"), True


def _title(row):
    for line in row["body"].splitlines():
        line = line.strip().lstrip("# ").strip()
        if line:
            return line[:76]
    return row["path"]


def build_view(result, runtime, now, since_days, display_name, body_limit=BODY_LIMIT):
    state = result["state"]
    logs, last_wakes = thread_logs(result, runtime)
    agents, health = presence(runtime, result, now, last_wakes, display_name)
    messages, full_text, threads = [], {}, {}
    since = now - timedelta(days=since_days) if since_days is not None else None
    week = now - timedelta(days=7)
    pending = state["pending"]
    pinned = {item["trigger_uuid"] for item in pending}
    by_thread = defaultdict(list)

    legacy = {row["filename"]: row for row in result["legacy_messages"]}
    legacy_group = {}
    for filename in legacy:
        trail, seen = [], set()
        target = filename
        while target not in legacy_group and target not in seen and target in legacy:
            trail.append(target)
            seen.add(target)
            target = legacy[target].get("re_resolved")
        if target in legacy_group:
            key = legacy_group[target]
        elif target in seen:
            key = "legacy:" + min(trail[trail.index(target):])
        else:
            key = "legacy:" + trail[-1]
        legacy_group.update(dict.fromkeys(trail, key))

    def append(row, at, source):
        if since is not None and at < since and row["id"] not in pinned:
            return
        row["at"] = board_lint.format_utc_rfc3339(at)
        row["recent"] = at >= week
        row["body"], truncated = _preview(source, body_limit)
        if truncated:
            row["more"] = True
        messages.append(row)
        by_thread[row["thread"]].append(row)
        full_text[row["path"]] = source

    for row in result["legacy_messages"]:
        meta = row["metadata"]
        stamp = datetime.fromtimestamp(row["mtime_ns"] / 1_000_000_000, timezone.utc)
        append({
            "id": row["filename"], "path": row["filename"], "number": row["number_str"],
            "from": meta.get("from", ""), "to": meta.get("to", ""),
            "type": (meta.get("type") or "legacy")[:48], "thread": legacy_group[row["filename"]],
            "needs": any(board_lint.is_yes(meta.get(field)) for field in board_lint.NEEDS_USER_FIELDS),
        }, stamp, row["body"])

    for row in result["v2_records"]:
        stamp = board_lint.parse_utc_rfc3339(row["sent_at"])
        item = {
            "id": row["uuid"], "path": row["filename"], "number": row["number_str"],
            "from": row["from"], "to": row["to"], "type": row["type"], "thread": row["thread"],
            "needs": row["needs_user"] == "yes",
        }
        for key in ("display_name", "decision", "action", "related_thread", "reason_code"):
            if row.get(key):
                item[key] = row[key]
        append(item, stamp, row["body"])

    def sort_key(row):
        return board_lint.parse_utc_rfc3339(row["at"]), row["number"], row["path"]

    messages.sort(key=sort_key)
    activity = {}
    for thread_id, rows in by_thread.items():
        rows.sort(key=sort_key)
        details = state["threads"].get(thread_id, {})
        task = state["tasks"].get(thread_id)
        root = next((row for row in rows if row["id"] == details.get("root_uuid")), rows[0])
        actors = {actor for row in rows for actor in (row["from"], row["to"]) if actor and actor != "all"}
        updated = rows[-1]["at"]
        activity[thread_id] = sort_key(rows[-1])
        threads[thread_id] = {
            "id": thread_id, "title": details.get("task_label") or _title(root),
            "at": updated, "actors": sorted(actors), "head": details.get("head_uuid"),
            "locked": bool(details.get("paused") or details.get("frozen") or details.get("terminal")),
        }
        if task:
            threads[thread_id]["task"] = {
                key: task[key] for key in ("kind", "state", "roles", "deliveries", "supersedes")
            }

    return {
        "messages": messages, "threads": [threads[key] for key in sorted(activity, key=activity.get, reverse=True)],
        "pending": pending, "logs": logs, "agents": agents, "health": health,
        "generated": board_lint.format_utc_rfc3339(now), "display_name": display_name,
        "human_roles": sorted(board_lint.VALID_SENDERS - board_lint.AGENT_PARTICIPANTS),
    }, full_text


class BoardView:
    """Cache the last stable scan and a few compact revisions for polling clients."""

    def __init__(self, board, runtime, since_days, display_name):
        self.board, self.runtime = Path(board), Path(runtime)
        self.since_days, self.display_name = since_days, display_name
        self.signature = None
        self.scanned_at = 0.0
        self.result = self.current = self.version = None
        self.full_text = {}
        self.revisions = OrderedDict()

    def refresh(self, force=False):
        fingerprint = inventory(self.board, self.runtime)
        clock = time.monotonic()
        if not force and fingerprint == self.signature and clock - self.scanned_at < 30:
            return
        now = datetime.now(timezone.utc)
        result = read_board(self.board, self.runtime, now)
        view, bodies = build_view(result, self.runtime, now, self.since_days, self.display_name)
        self.result = result
        if not result["health"]["healthy"] and self.current is not None:
            # Keep the last readable history during a transient scan failure.
            health = view["health"]
            view = {**self.current, "health": health, "generated": view["generated"]}
        else:
            self.signature = fingerprint
            self.scanned_at = clock
            self.full_text = bodies
        versioned = {key: value for key, value in view.items() if key != "generated"}
        revision = hashlib.sha256(json_bytes(versioned)).hexdigest()[:24]
        if revision != self.version:
            self.current, self.version = view, revision
            self.revisions[revision] = view
            self.revisions.move_to_end(revision)
            if len(self.revisions) > 4:
                self.revisions.popitem(last=False)

    def changes(self, since):
        self.refresh()
        if since == self.version:
            return {"version": self.version, "changed": False}
        if since not in self.revisions:
            return {"version": self.version, "changed": True, "reset": self.current}
        old = self.revisions[since]
        delta = {key: value for key, value in self.current.items() if key != "messages" and value != old[key]}
        before = {message["id"]: message for message in old["messages"]}
        after = {message["id"]: message for message in self.current["messages"]}
        return {
            "version": self.version, "changed": True, "delta": delta,
            "upsert": [message for key, message in after.items() if message != before.get(key)],
            "remove": list(before.keys() - after.keys()),
        }
