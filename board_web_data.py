"""Read-only BoardHub Web projections shared by the server and offline export."""

from bisect import bisect_left
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from threading import Event, Thread
import time

import board_lint

board_state = board_lint.load_sibling_module("board_state")
board_watch = board_lint.load_sibling_module("board_watch")
wake_routes = board_lint.load_sibling_module("wake_routes")
claude_transcript = board_lint.load_sibling_module("claude_transcript")

BODY_LIMIT = 4096
PAGE_LIMIT = 3_000_000
DISCOVERY_INTERVAL = 60
DISCOVERY_TIMEOUT = 0.8
RUNTIME_FILES = (
    "manifest.json", "baseline.json", "housekeeping.json", "health.json",
    "registry.json", "routes.json", "watch.log", "wake_ledger.json", "mail_pending.json",
    "group.json",
)

DEFAULT_GROUP = {"id": "default", "name": "BoardHub"}
DEFAULT_MEMBERS = {
    "claude_code": {
        "name": "Claude Code", "short": "CC", "kind": "agent", "color": "#d97757", "adapter": "claude",
        "aliases": {"claude": "main", "claude_main": "main", "claude_side": "side"},
    },
    "antigravity": {
        "name": "Antigravity", "short": "AG", "kind": "agent", "color": "#3b82f6", "adapter": "gemini_agy",
        "aliases": {"gemini": "default"},
    },
    "codex": {
        "name": "Codex", "short": "CX", "kind": "agent", "color": "#10a37f", "adapter": "codex",
        "aliases": {"codex": None, "gpt": "gpt"},
    },
    "dsh": {
        "name": "DSH", "short": "DS", "kind": "agent", "color": "#8b5cf6", "adapter": "dsh",
        "aliases": {"dsh": None},
    },
    "grogan": {
        "name": "Grogan", "short": "GR", "kind": "human", "color": "#f59e0b", "adapter": "human",
        "aliases": {"grogan": None, "user": "web"},
    },
}
ALIASES = {agent: (member, label) for member, config in DEFAULT_MEMBERS.items()
           for agent, label in config["aliases"].items()}
DEFAULT_ROLES = {
    "coordinate": [{"member": "claude_code", "slot": "main"}, {"member": "claude_code", "slot": "side"}],
    "review": [{"member": "codex"}, {"member": "antigravity"}],
    "implement": [{"member": "codex"}, {"member": "dsh"}, {"member": "claude_code", "slot": "side"}],
    "run": [{"member": "dsh"}],
    "literature": [{"member": "antigravity"}, {"member": "claude_code", "slot": "side"}],
    "decide": [{"member": "grogan"}],
}
ATTRIBUTION_HEADERS = frozenset({"member", "session_id", "session_label", "model", "effort", "sender_role"})
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
ROLE_BY_TYPE = {"task": "coordinate", "review": "review", "decision": "decide"}
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
ROLE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
AGY_MODEL_RE = re.compile(r"(gemini-4-(?:carbon|argon))-(low|medium|high)")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="replace")


def private_json(path):
    try:
        return board_lint.read_private_json_file(path) or {}
    except (board_lint.SecurityError, board_lint.FormatError, OSError):
        return {}


def read_group(runtime, fallback):
    config = private_json(runtime / "group.json")
    if all(isinstance(config.get(key), str) and config[key].strip() for key in DEFAULT_GROUP):
        return {key: config[key].strip() for key in DEFAULT_GROUP}
    fallback = fallback or {}
    return {key: (fallback.get(key) or "").strip() or default for key, default in DEFAULT_GROUP.items()}


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
    result = board_state.rebuild(
        snapshot, manifest or {}, now, housekeeping_ids=dismissed, baseline_uuids=baseline,
        runtime_corrupt=manifest_bad or receipts_bad or baseline_bad,
    )
    # The state projection drops session headers. Read just those fields from the
    # already validated v2 records in the same safe snapshot.
    for row in result["v2_records"]:
        raw = snapshot.entries[row["filename"]].raw_bytes
        frontmatter = raw[4:].split(b"\n---\n", 1)[0].decode("utf-8")
        row["attribution_headers"] = {
            key: value.strip(" ") for key, _, value in
            (line.partition(":") for line in frontmatter.split("\n")) if key in ATTRIBUTION_HEADERS
        }
    return result


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


def thread_logs(result, runtime, dispatches=None):
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
        stamp = board_lint.parse_utc_rfc3339(at)
        if not stamp or not wake_routes.SAFE_CODE_RE.fullmatch(status):
            continue
        if route == "mail":
            thread = item_threads.get(row.get("item")) or row.get("thread")
        elif agent in board_lint.AGENT_PARTICIPANTS and wake_routes.SAFE_CODE_RE.fullmatch(route):
            thread = message_threads.get(row.get("uuid"))
            if status == "accepted" and at > latest.get(agent, ""):
                latest[agent] = at
            if dispatches is not None and thread and status == "accepted":
                dispatches.append((board_lint.format_utc_rfc3339(stamp), row["uuid"]))
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


def presence(runtime, result, now, latest, display_name, registry, registry_bad, wakes):
    routes, routes_bad = wake_routes.load_route_config(runtime / "routes.json")
    for record in wakes:
        agent = record.get("agent")
        if isinstance(agent, str) and agent in board_lint.AGENT_PARTICIPANTS:
            for attempt in accepted_attempts(record):
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


def accepted_attempts(record):
    attempts = record.get("attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get("outcome") == "accepted":
                yield attempt


def session_metadata(raw, member):
    fields = {}
    session_id = raw.get("session_id")
    adapter = DEFAULT_MEMBERS[member]["adapter"]
    pattern = board_lint.SESSION_ID_PATTERNS.get(adapter, board_lint.CONVERSATION_ID_RE)
    if isinstance(session_id, str) and pattern.fullmatch(session_id):
        fields["session_id"] = session_id
    label = raw.get("session_label")
    if (isinstance(label, str) and label.strip() and len(label.encode("utf-8", "replace")) <= 80
            and not board_lint.has_disallowed_control_chars(label, allow_newlines=False)):
        fields["session_label"] = label
    model = raw.get("model")
    if isinstance(model, str) and MODEL_RE.fullmatch(model):
        fields["model"] = model
        bundled = AGY_MODEL_RE.fullmatch(model) if member == "antigravity" else None
        if bundled:
            fields["model"], fields["effort"] = bundled.groups()
    effort = raw.get("effort")
    if effort in EFFORTS:
        fields["effort"] = effort
    return fields


def session_status(now, seen, confirmed=True):
    if not confirmed or seen is None or now - seen >= timedelta(days=1):
        return "stale"
    return "live" if now - seen < timedelta(minutes=2) else "idle"


def registry_activity(record):
    parse_time = board_lint.parse_utc_rfc3339
    checked_in = parse_time(record["checked_in_at"])
    return max(checked_in, parse_time(record["last_successful_wake_at"]) or checked_in)


def discovery_statuses(checkins, registry, now):
    """Find the latest check-in state for a session that discovery can identify."""
    statuses = {}
    parse_time = board_lint.parse_utc_rfc3339
    for agent, events in checkins.items():
        member, _ = ALIASES[agent]
        record = registry.roles.get(agent)
        registry_key = (parse_time(record["sent_at"]), record["filename"]) if record else None
        bound = None
        for key, fields in events:
            session_id = fields.get("session_id")
            if key == registry_key and record["session_id"]:
                session_id = record["session_id"]
                status = session_status(now, registry_activity(record), record["confirmed"])
                bound = session_id if record["confirmed"] else None
            elif session_id:
                status = session_status(now, key[0])
                bound = session_id
            else:
                session_id, bound = bound, None
                status = "stale"
            if session_id:
                identity = member, session_id
                previous = statuses.get(identity)
                if previous is None or key > previous[0]:
                    statuses[identity] = key, status
    return statuses


def known_sessions(result, registry, wakes, now, discovered):
    sessions = defaultdict(dict)
    checkins, spawns = defaultdict(dict), defaultdict(dict)

    def add(member, label, fields, seen, source, confirmed=True):
        session_id = fields["session_id"]
        row = {
            "id": session_id, "label": fields.get("session_label") or label or "#" + session_id[:8],
            "last_seen": board_lint.format_utc_rfc3339(seen), "source": source,
            "status": session_status(now, seen, confirmed),
        }
        row.update((key, fields[key]) for key in ("model", "effort") if key in fields)
        previous = sessions[member].get(session_id)
        if previous is None or (seen, source == "registry") > previous[0]:
            sessions[member][session_id] = (seen, source == "registry"), row

    parse_time = board_lint.parse_utc_rfc3339
    for row in result["v2_records"]:
        agent = row["from"]
        if row["status"] != "accepted" or row["type"] not in board_lint.REGISTRY_EVENT_TYPES:
            continue
        member, _ = ALIASES[agent]
        fields = session_metadata(row.get("attribution_headers", {}), member)
        key = parse_time(row["sent_at"]), row["filename"]
        checkins[agent][key] = fields if row["type"] == "checkin" and fields.get("session_id") else {}

    for agent, record in registry.roles.items():
        member, label = ALIASES[agent]
        key = parse_time(record["sent_at"]), record["filename"]
        fields = session_metadata(record, member)
        if "session_id" in fields:
            # A matching board check-in can carry additional frontmatter.
            board_fields = checkins[agent].get(key, {})
            if board_fields.get("session_id") == fields["session_id"]:
                fields = {**board_fields, **fields}
            add(member, label, fields, registry_activity(record), "registry", record["confirmed"])
        checkins[agent][key] = fields if record["confirmed"] else {}

    for record in wakes:
        agent, spawn = record.get("agent"), record.get("spawn")
        if agent not in ("codex", "dsh") or not isinstance(spawn, dict) or spawn.get("adapter") != agent:
            continue
        attempt_at = parse_time(record.get("last_attempt_at"))
        thread, trigger = spawn.get("thread_id"), record.get("message_uuid")
        if not (attempt_at and isinstance(thread, str) and board_lint.UUID_V4_RE.fullmatch(thread)
                and isinstance(trigger, str) and board_lint.UUID_V4_RE.fullmatch(trigger)):
            continue
        outcome = record.get("status")
        if outcome != "accepted" and not (agent == "codex" and outcome == "attempting"):
            continue
        fields = session_metadata(spawn, agent)
        if "session_id" not in fields:
            continue
        seen = attempt_at
        if outcome == "accepted":
            for attempt in accepted_attempts(record):
                finished = parse_time(attempt.get("finished_at"))
                if finished:
                    seen = max(seen, finished)
            spawns[(agent, thread)][(attempt_at, trigger)] = fields
        add(agent, None, fields, seen, "wake_ledger")

    checkins = {agent: sorted(events.items()) for agent, events in checkins.items()}
    statuses = discovery_statuses(checkins, registry, now) if any(discovered.values()) else {}
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    members = []
    for member, config in DEFAULT_MEMBERS.items():
        known = {session_id: row for session_id, (_, row) in sessions[member].items()}
        for found in discovered.get(member, ()):
            session_id = found["id"]
            # The existing projection owns provenance and last_seen for known sessions.
            merged = {**found, **known.get(session_id, {})}
            evidence = statuses.get((member, session_id))
            if evidence:
                merged["status"] = evidence[1]
            known[session_id] = merged
        rows = [known[session_id] for session_id in sorted(known)]
        rows.sort(key=lambda row: parse_time(row.get("last_seen")) or minimum, reverse=True)
        members.append({"id": member, **{key: config[key] for key in ("name", "short", "kind", "color")},
                        "sessions": rows})
    return members, checkins, {
        key: sorted(events.items()) for key, events in spawns.items()
    }


def attribution(row, meta, at, checkins, spawns, *, v2=False):
    agent = row["from"].strip().lower()
    member, label = ALIASES.get(agent, (None, None))
    explicit_member = meta.get("member")
    if isinstance(explicit_member, str) and explicit_member in DEFAULT_MEMBERS and member in (None, explicit_member):
        member = explicit_member
    else:
        explicit_member = None
    if member is None:
        return None
    explicit = session_metadata(meta, member)
    role = meta.get("sender_role")
    if isinstance(role, str) and ROLE_RE.fullmatch(role) and (role != "decide" or member == "grogan"):
        explicit["role"] = role

    evidence = {}
    if v2:
        candidates = ((spawns.get((agent, row["thread"]), ()), "\uffff"),
                      (checkins.get(agent, ()), row["path"] + "\x00"))
        for timeline, tie in candidates:
            position = bisect_left(timeline, ((at, tie),))
            if position:
                candidate = timeline[position - 1][1]
                session_id = candidate.get("session_id")
                if session_id and explicit.get("session_id", session_id) == session_id:
                    evidence = candidate
                    break

    fields = {**evidence, **explicit}
    if "session_label" not in fields:
        if "session_id" in fields:
            fields["session_label"] = label or "#" + fields["session_id"][:8]
        elif label and agent != "gemini":
            fields["session_label"] = label
    inferred_role = ROLE_BY_TYPE.get(row["type"])
    if v2 and row["type"] == "delivery":
        inferred_role = "implement"
    if inferred_role and (inferred_role != "decide" or member == "grogan"):
        fields.setdefault("role", inferred_role)
    confidence = "explicit" if explicit or explicit_member else ("registry" if evidence else "inferred")
    return {"member": member, **fields, "confidence": confidence}


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


def build_view(result, runtime, now, since_days, display_name, body_limit=BODY_LIMIT, *,
               discovered=None, dispatches=None, group_fallback=None):
    state = result["state"]
    logs, last_wakes = thread_logs(result, runtime, dispatches)
    registry, registry_bad = wake_routes.RoleRegistry.load(runtime / "registry.json")
    raw_wakes = private_json(runtime / "wake_ledger.json").get("records")
    wakes = [record for record in raw_wakes.values() if isinstance(record, dict)] if isinstance(raw_wakes, dict) else []
    if dispatches is not None:
        for record in wakes:
            message_id = record.get("message_uuid")
            if not isinstance(message_id, str):
                continue
            for attempt in accepted_attempts(record):
                at = board_lint.parse_utc_rfc3339(attempt.get("at"))
                if at:
                    dispatches.append((board_lint.format_utc_rfc3339(at), message_id))
    agents, health = presence(runtime, result, now, last_wakes, display_name, registry, registry_bad, wakes)
    members, checkins, spawns = known_sessions(result, registry, wakes, now, discovered or {})
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

    def append(row, at, source, meta, *, v2=False):
        if since is not None and at < since and row["id"] not in pinned:
            return
        sender = attribution(row, meta, at, checkins, spawns, v2=v2)
        if sender:
            row["attribution"] = sender
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
        }, stamp, row["body"], meta)

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
        append(item, stamp, row["body"], row.get("attribution_headers", {}), v2=True)

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
        "group": read_group(runtime, group_fallback), "members": members,
        "roles": {role: [dict(target) for target in targets] for role, targets in DEFAULT_ROLES.items()},
        "generated": board_lint.format_utc_rfc3339(now), "display_name": display_name,
        "human_roles": sorted(board_lint.VALID_SENDERS - board_lint.AGENT_PARTICIPANTS),
    }, full_text


def read_discovered_sessions(board):
    try:
        discovery = board_lint.load_sibling_module("session_discovery")
        parent = Path(board).resolve().parent
        project = parent.parent if parent.name == "bridge" else parent
        return discovery.discover_sessions(project_dir=project, timeout=DISCOVERY_TIMEOUT)
    except Exception:
        # Discovery is optional. The caller can keep serving its last snapshot.
        return None


def new_mainline_reader():
    return claude_transcript.ClaudeTranscripts(board_lint.load_sibling_module("session_discovery"))


def read_mainline(reader, runtime, references, now, since_days):
    try:
        config = board_lint.read_private_json_file(
            Path(runtime) / "mainline.json", max_bytes=claude_transcript.CONFIG_LIMIT, max_depth=5
        )
    except (board_lint.SecurityError, board_lint.FormatError, OSError):
        config = None
    since = now - timedelta(days=since_days) if since_days is not None else None
    return reader.read(config, references, since)


class BoardView:
    """Cache the last stable scan and a few compact revisions for polling clients."""

    def __init__(self, board, runtime, since_days, display_name, *, group_fallback=None):
        self.board, self.runtime = Path(board), Path(runtime)
        self.since_days, self.display_name = since_days, display_name
        self.group_fallback = group_fallback
        self.signature = None
        self.scanned_at = 0.0
        self.result = self.current = self.version = None
        self.board_view = self.applied_board_view = None
        self.full_text = {}
        self.revisions = OrderedDict()
        self.discovered = self.applied_discovery = {}
        self.discovery_stop = Event()
        self.transcripts = new_mainline_reader()
        self.references = claude_transcript.BoardReferences()

    def start_discovery(self):
        Thread(target=self._discover_periodically, name="BoardHub session discovery", daemon=True).start()

    def _discover_periodically(self):
        while not self.discovery_stop.is_set():
            discovered = read_discovered_sessions(self.board)
            if discovered is not None and discovered != self.discovered:
                # Publish a complete snapshot without taking the request lock.
                self.discovered = discovered
            self.discovery_stop.wait(DISCOVERY_INTERVAL)

    def refresh(self, force=False):
        fingerprint = inventory(self.board, self.runtime)
        clock = time.monotonic()
        discovered = self.discovered
        if (not force and fingerprint == self.signature and discovered is self.applied_discovery
                and clock - self.scanned_at < 30):
            return
        now = datetime.now(timezone.utc)
        result = read_board(self.board, self.runtime, now)
        dispatches = []
        view, bodies = build_view(result, self.runtime, now, self.since_days, self.display_name,
                                  discovered=discovered, dispatches=dispatches, group_fallback=self.group_fallback)
        self.result = result
        if not result["health"]["healthy"] and self.board_view is not None:
            # Keep the last readable history during a transient scan failure.
            view = {**self.board_view, "group": view["group"], "health": view["health"], "generated": view["generated"]}
        else:
            self.signature = fingerprint
            self.scanned_at = clock
            self.applied_discovery = discovered
            self.full_text = bodies
            self.references = claude_transcript.BoardReferences(view["messages"], dispatches)
        self.board_view = view

    def changes(self, since):
        self.refresh()
        mainline = read_mainline(self.transcripts, self.runtime, self.references,
                                 datetime.now(timezone.utc), self.since_days)
        if self.board_view is not self.applied_board_view or mainline != self.current["mainline"]:
            self._publish(mainline)
            self.applied_board_view = self.board_view
        if since == self.version:
            return {"version": self.version, "changed": False}
        if since not in self.revisions:
            return {"version": self.version, "changed": True, "reset": self.current}
        old = self.revisions[since]
        base = self.revisions[self.version]
        delta = {key: self.current[key] for key, value in base.items()
                 if key != "messages" and value != old.get(key)}
        before = {message["id"]: message for message in old["messages"]}
        after = {message["id"]: message for message in self.current["messages"]}
        return {
            "version": self.version, "changed": True, "delta": delta,
            "upsert": [message for key, message in after.items() if message != before.get(key)],
            "remove": list(before.keys() - after.keys()),
        }

    def _publish(self, mainline):
        view = {**self.board_view, "mainline": mainline}
        # Keep only the digest of earlier transcript views in the polling history.
        mainline_digest = hashlib.sha256(json_bytes(mainline)).hexdigest()
        compact = {**view, "mainline": mainline_digest}
        versioned = {key: value for key, value in compact.items() if key != "generated"}
        revision = hashlib.sha256(json_bytes(versioned)).hexdigest()[:24]
        if revision != self.version:
            self.current, self.version = view, revision
            self.revisions[revision] = compact
            self.revisions.move_to_end(revision)
            if len(self.revisions) > 4:
                self.revisions.popitem(last=False)
