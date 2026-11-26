#!/usr/bin/env python3
"""BoardHub deterministic state rebuild, _hub projection publisher, and safe v2 message writer.

Python 3.8+ standard library only.
"""

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
from typing import Dict, List, Optional, Set, Tuple

# Support python3 -I (isolated mode) by loading sibling board_lint safely.
if "board_lint" in sys.modules:
    board_lint = sys.modules["board_lint"]
else:
    _lint_path = Path(__file__).resolve().parent / "board_lint.py"
    _fd = os.open(_lint_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _st = os.fstat(_fd)
        if not stat.S_ISREG(_st.st_mode) or _st.st_uid != os.getuid() or (stat.S_IMODE(_st.st_mode) & 0o022):
            raise RuntimeError("Unsafe sibling board_lint.py")
    finally:
        os.close(_fd)
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("board_lint", str(_lint_path))
    board_lint = _ilu.module_from_spec(_spec)
    sys.modules["board_lint"] = board_lint
    _spec.loader.exec_module(board_lint)


def get_inbox_recipients() -> Tuple[str, ...]:
    """Return sorted valid inbox recipients including the active configured human role."""
    return tuple(sorted(board_lint.VALID_SENDERS))


SCHEMA_VERSION = 2
INBOX_RECIPIENTS = get_inbox_recipients()
SEVERITY_ORDER = {"high": 0, "error": 1, "warning": 2}
_PUBLISH_LOCKS_GUARD = threading.Lock()
_PUBLISH_LOCKS: Dict[Tuple[str, str], threading.Lock] = {}


class PublishError(Exception):
    """Raised when publishing a v2 message or projection fails."""


class DurabilityError(PublishError):
    """Raised when fsync / rename durability fails explicitly."""


class RequestConflictError(PublishError):
    """Raised when publishing an existing UUID with different bytes or route."""


# --- Manifest & housekeeping APFS helpers ---

def _extract_manifest_entries(manifest) -> Tuple[Dict[str, dict], bool]:
    """Normalize manifest dict into (entries_map, active_baseline_present)."""
    if not manifest:
        return {}, False
    if not isinstance(manifest, dict):
        raise board_lint.FormatError("RUNTIME_CORRUPT", "Manifest must be a JSON object.")
    if "entries" in manifest:
        entries = manifest["entries"]
        if not isinstance(entries, dict):
            raise board_lint.FormatError("RUNTIME_CORRUPT", "Manifest entries must be an object.")
        return entries, True
    # Allow direct {filename: entry} mapping if passed in tests
    return manifest, bool(manifest)


def load_manifest(manifest_path: Path) -> Tuple[Optional[dict], bool]:
    """Load APFS manifest JSON. Returns (manifest_dict_or_none, corrupt_flag)."""
    try:
        obj = board_lint.read_private_json_file(manifest_path)
    except (board_lint.SecurityError, board_lint.FormatError, OSError):
        return None, True
    if obj is None:
        return None, False
    if obj.get("version") != SCHEMA_VERSION or not isinstance(obj.get("entries"), dict):
        return None, True
    return obj, False


def save_manifest(manifest_path: Path, manifest: dict) -> None:
    payload = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    board_lint.atomic_write_private_file(manifest_path, payload, mode=0o600)


def load_housekeeping_receipts(path: Path) -> Tuple[Set[str], dict, bool]:
    """Load APFS housekeeping receipts keyed by exact pending_id."""
    try:
        obj = board_lint.read_private_json_file(path)
    except (board_lint.SecurityError, board_lint.FormatError, OSError):
        return set(), {}, True
    if obj is None:
        return set(), {"version": SCHEMA_VERSION, "receipts": {}}, False
    receipts = obj.get("receipts")
    if obj.get("version") != SCHEMA_VERSION or not isinstance(receipts, dict):
        return set(), {}, True
    for k, v in receipts.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            return set(), {}, True
    return set(receipts.keys()), obj, False


def add_housekeeping_receipt(path: Path, pending_id: str, note: str = "", as_of: Optional[datetime] = None) -> dict:
    """Persist an APFS housekeeping receipt hiding pending_id from open pending list."""
    if not isinstance(pending_id, str) or not pending_id or board_lint.has_disallowed_control_chars(pending_id):
        raise ValueError("Invalid pending_id.")
    if board_lint.has_disallowed_control_chars(note):
        raise ValueError("Invalid note.")
    _, obj, corrupt = load_housekeeping_receipts(path)
    if corrupt:
        raise board_lint.FormatError("RUNTIME_CORRUPT", "Housekeeping receipts file is corrupt.")
    ts = board_lint.format_utc_rfc3339(as_of or datetime.now(timezone.utc))
    receipts = dict(obj["receipts"])
    if pending_id not in receipts:
        receipts[pending_id] = {
            "dismissed_at": ts,
            "note": note[:240],
        }
    updated = {"version": SCHEMA_VERSION, "receipts": receipts}
    payload = (json.dumps(updated, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    board_lint.atomic_write_private_file(path, payload, mode=0o600)
    return updated


def build_manifest_from_snapshot(
    snapshot: board_lint.BoardSnapshot,
    as_of: datetime,
    existing_manifest: Optional[dict] = None,
    reconcile: bool = False,
) -> dict:
    """Create or update a manifest from a healthy scan snapshot."""
    if not snapshot.healthy:
        raise RuntimeError("Cannot build manifest from unhealthy snapshot.")
    now_str = board_lint.format_utc_rfc3339(as_of)
    existing_entries, _ = _extract_manifest_entries(existing_manifest)
    new_entries: Dict[str, dict] = {}
    for fname in sorted(snapshot.entries):
        entry = snapshot.entries[fname]
        prev_entry = None if reconcile else existing_entries.get(fname)
        if prev_entry and prev_entry.get("sha256") == entry.sha256:
            new_entries[fname] = prev_entry
            continue
        first_obs = (
            prev_entry.get("first_observed_at", now_str)
            if (prev_entry and isinstance(prev_entry, dict))
            else now_str
        )
        msg_uuid = None
        thr_uuid = None
        kind = "legacy"
        if board_lint.is_v2_candidate_filename(fname):
            kind = "v2"
            parsed, diags = board_lint.parse_v2_message(entry)
            if parsed is not None:
                msg_uuid = parsed.request_id
                thr_uuid = parsed.thread
            elif diags:
                msg_uuid = diags[0].message_id
                thr_uuid = diags[0].thread_id
                kind = "invalid_v2"
        new_entries[fname] = {
            "sha256": entry.sha256,
            "first_observed_at": first_obs,
            "kind": kind,
            "uuid": msg_uuid,
            "thread": thr_uuid,
        }
    return {
        "version": SCHEMA_VERSION,
        "bootstrapped_at": (
            existing_manifest.get("bootstrapped_at", now_str)
            if (existing_manifest and not reconcile)
            else now_str
        ),
        "reconciled_at": now_str if reconcile else (existing_manifest.get("reconciled_at") if existing_manifest else None),
        "entries": new_entries,
    }


# --- Helpers ---

def _compute_snapshot_id(snapshot: board_lint.BoardSnapshot, as_of_str: str) -> str:
    h = hashlib.sha256()
    h.update(b"boardhub-v2\n")
    h.update(as_of_str.encode("utf-8") + b"\n")
    h.update((b"healthy\n" if snapshot.healthy else b"unhealthy\n"))
    for fname in sorted(snapshot.entries):
        e = snapshot.entries[fname]
        h.update(f"E:{fname}:{e.sha256}:{e.size}\n".encode("utf-8"))
    for fname in sorted(snapshot.unsafe_entries):
        u = snapshot.unsafe_entries[fname]
        h.update(f"U:{fname}:{u.reason}\n".encode("utf-8"))
    return h.hexdigest()[:16]


def _make_pending_id(thread_id: str, reason: str, trigger_uuid: str) -> str:
    return f"{thread_id}:{reason}:{trigger_uuid}"


def _make_incident_id(diag: board_lint.Diagnostic) -> str:
    raw = f"{diag.code}|{diag.file}|{diag.message_id or ''}|{diag.thread_id or ''}".encode("utf-8")
    return f"incident:{diag.code}:{hashlib.sha256(raw).hexdigest()[:12]}"


def _diag_sort_key(d: board_lint.Diagnostic) -> tuple:
    return (
        SEVERITY_ORDER.get(d.severity, 9),
        d.code,
        d.file,
        d.thread_id or "",
        d.message_id or "",
    )


def _legacy_re_target(re_raw: str) -> Optional[str]:
    if not re_raw:
        return None
    val = re_raw.strip()
    if val.lower() in ("none", ""):
        return None
    if board_lint.LEGACY_RE_NUM_RE.fullmatch(val):
        return val
    return None


def _parse_as_of(as_of) -> datetime:
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None:
            return as_of.replace(tzinfo=timezone.utc)
        return as_of.astimezone(timezone.utc)
    if isinstance(as_of, str):
        parsed = board_lint.parse_utc_rfc3339(as_of)
        if parsed is not None:
            return parsed
    raise ValueError("as_of must be a UTC datetime or RFC 3339 string.")


def _first_observed_dt(filename: str, manifest_entries: dict, as_of_dt: datetime) -> datetime:
    mentry = manifest_entries.get(filename)
    if isinstance(mentry, dict):
        ts = mentry.get("first_observed_at")
        parsed = board_lint.parse_utc_rfc3339(ts) if isinstance(ts, str) else None
        if parsed is not None:
            return parsed
    return as_of_dt


# --- Pure deterministic state rebuild ---

def rebuild(
    snapshot: board_lint.BoardSnapshot,
    manifest,
    as_of,
    housekeeping_ids: Optional[Set[str]] = None,
    baseline_uuids: Optional[Set[str]] = None,
    runtime_corrupt: bool = False,
) -> dict:
    """Deterministic pure projection: rebuild(snapshot, manifest, as_of) -> state, lint, digest."""
    as_of_dt = _parse_as_of(as_of)
    as_of_str = board_lint.format_utc_rfc3339(as_of_dt)
    snapshot_id = _compute_snapshot_id(snapshot, as_of_str)
    dismissed_ids = set(housekeeping_ids or ())
    known_baseline_uuids = set(baseline_uuids or ())

    diagnostics: List[board_lint.Diagnostic] = []
    manifest_corrupt = False
    try:
        manifest_entries, manifest_active = _extract_manifest_entries(manifest)
    except board_lint.FormatError:
        manifest_entries, manifest_active = {}, False
        manifest_corrupt = True

    if runtime_corrupt or manifest_corrupt:
        diagnostics.append(board_lint.Diagnostic(severity="high", code="RUNTIME_CORRUPT", file="_runtime"))

    if not snapshot.healthy:
        diagnostics.append(board_lint.Diagnostic(severity="high", code="SCAN_UNAVAILABLE", file="_board"))
        diagnostics.sort(key=_diag_sort_key)
        diag_dicts = [d.to_dict() for d in diagnostics]
        counts = {
            "high": sum(1 for d in diagnostics if d.severity == "high"),
            "error": sum(1 for d in diagnostics if d.severity == "error"),
            "warning": sum(1 for d in diagnostics if d.severity == "warning"),
        }
        lint_report = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "as_of": as_of_str,
            "exit_code": 2,
            "counts": counts,
            "diagnostics": diag_dicts,
        }
        health = {
            "healthy": False,
            "stale": True,
            "scan_healthy": False,
            "runtime_corrupt": bool(runtime_corrupt or manifest_corrupt),
            "live_dispatch_suppressed": True,
            "reason": snapshot.scan_error or "scan_unavailable",
        }
        return {
            "snapshot_id": snapshot_id,
            "as_of": as_of_str,
            "health": health,
            "lint": lint_report,
            "state": {
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "as_of": as_of_str,
                "health": health,
                "tasks": {},
                "threads": {},
                "pending": [],
                "inboxes": {a: [] for a in get_inbox_recipients()},
            },
            "mail_pending": None,
            "legacy_messages": [],
            "v2_records": [],
        }

    # --- 1. Unsafe entries & Immutability checks BEFORE parsing changed files ---
    frozen_threads: Dict[str, List[str]] = {}
    unknown_scope_freeze = bool(runtime_corrupt or manifest_corrupt)
    immutability_blocked_files: Set[str] = set()

    def _freeze_thread(tid: Optional[str], reason: str) -> None:
        nonlocal unknown_scope_freeze
        if tid and board_lint.UUID_V4_RE.fullmatch(tid):
            frozen_threads.setdefault(tid, [])
            if reason not in frozen_threads[tid]:
                frozen_threads[tid].append(reason)
        else:
            unknown_scope_freeze = True

    for uname in sorted(snapshot.unsafe_entries):
        diagnostics.append(board_lint.Diagnostic(severity="high", code="UNSAFE_ENTRY", file=uname))

    if manifest_active:
        for fname in sorted(manifest_entries):
            mentry = manifest_entries[fname]
            if not isinstance(mentry, dict) or not isinstance(mentry.get("sha256"), str):
                diagnostics.append(board_lint.Diagnostic(severity="high", code="RUNTIME_CORRUPT", file=fname))
                unknown_scope_freeze = True
                continue
            m_uuid = mentry.get("uuid")
            m_thread = mentry.get("thread")
            if fname not in snapshot.entries:
                # Missing or replaced with unsafe entry
                diagnostics.append(
                    board_lint.Diagnostic(
                        severity="high",
                        code="IMMUTABILITY",
                        file=fname,
                        message_id=m_uuid,
                        thread_id=m_thread,
                    )
                )
                immutability_blocked_files.add(fname)
                _freeze_thread(m_thread, "IMMUTABILITY")
            elif snapshot.entries[fname].sha256 != mentry["sha256"]:
                # Modified file: do NOT parse the changed bytes
                diagnostics.append(
                    board_lint.Diagnostic(
                        severity="high",
                        code="IMMUTABILITY",
                        file=fname,
                        message_id=m_uuid,
                        thread_id=m_thread,
                    )
                )
                immutability_blocked_files.add(fname)
                _freeze_thread(m_thread, "IMMUTABILITY")

    # --- 2. Classify & parse remaining entries ---
    legacy_messages: List[dict] = []
    legacy_by_num: Dict[str, List[str]] = {}
    legacy_by_name: Dict[str, dict] = {}
    number_groups: Dict[str, List[Tuple[str, Optional[str]]]] = {}

    v2_candidates_raw: List[Tuple[board_lint.SnapshotEntry, Optional[board_lint.ParsedV2Message], List[board_lint.Diagnostic], Optional[str]]] = []

    for fname in sorted(snapshot.entries):
        if fname in immutability_blocked_files:
            continue
        entry = snapshot.entries[fname]

        if board_lint.is_v2_candidate_filename(fname):
            parsed, diags = board_lint.parse_v2_message(entry)
            m_cand = board_lint.V2_CANDIDATE_FILENAME_RE.fullmatch(fname)
            uuid_hint = None
            if m_cand and board_lint.UUID_V4_RE.fullmatch(m_cand[3]):
                uuid_hint = m_cand[3]
            if parsed is not None:
                uuid_hint = parsed.request_id
            v2_candidates_raw.append((entry, parsed, diags, uuid_hint))
            m_num = re.match(r"^([0-9]{4})_", fname)
            if m_num:
                number_groups.setdefault(m_num[1], []).append((fname, uuid_hint))
            continue

        m_legacy = board_lint.LEGACY_FILENAME_RE.fullmatch(fname)
        if m_legacy:
            lmsg = board_lint.parse_legacy_message(entry)
            if lmsg.decode_error:
                diagnostics.append(board_lint.Diagnostic(severity="error", code="FORMAT", file=fname))
                continue
            if lmsg.metadata.get("v") == "2":
                # Legacy-named file claiming v: 2 without --<uuid> filename suffix
                diagnostics.append(board_lint.Diagnostic(severity="error", code="FORMAT", file=fname))
                continue
            legacy_by_num.setdefault(lmsg.number_str, []).append(fname)
            number_groups.setdefault(lmsg.number_str, []).append((fname, None))
            ldict = {
                "kind": "legacy",
                "filename": lmsg.filename,
                "number": lmsg.number,
                "number_str": lmsg.number_str,
                "sha256": lmsg.sha256,
                "mtime_ns": lmsg.mtime_ns,
                "metadata": dict(lmsg.metadata),
                "body": lmsg.body,
                "re_resolved": None,
                "re_ambiguous": False,
            }
            legacy_messages.append(ldict)
            legacy_by_name[fname] = ldict
        else:
            diagnostics.append(board_lint.Diagnostic(severity="error", code="FORMAT", file=fname))

    # Resolve legacy numeric replies (only when matching a single legacy message)
    for ldict in legacy_messages:
        re_raw = ldict["metadata"].get("re", "")
        num_ref = _legacy_re_target(re_raw)
        if num_ref is not None:
            matches = legacy_by_num.get(num_ref, [])
            if len(matches) == 1:
                ldict["re_resolved"] = matches[0]
            elif len(matches) > 1:
                ldict["re_ambiguous"] = True
                diagnostics.append(
                    board_lint.Diagnostic(
                        severity="warning",
                        code="LEGACY_REF_AMBIGUOUS",
                        file=ldict["filename"],
                    )
                )
        elif re_raw and re_raw.strip() in legacy_by_name:
            ldict["re_resolved"] = re_raw.strip()

    # --- 3. Group v2 candidates by UUID: RETRY_ALIAS vs REQUEST_CONFLICT ---
    by_uuid: Dict[str, List[Tuple[board_lint.SnapshotEntry, Optional[board_lint.ParsedV2Message], List[board_lint.Diagnostic]]]] = {}
    v2_without_uuid: List[Tuple[board_lint.SnapshotEntry, List[board_lint.Diagnostic]]] = []
    for entry, parsed, diags, uuid_hint in v2_candidates_raw:
        if uuid_hint is None:
            v2_without_uuid.append((entry, diags))
        else:
            by_uuid.setdefault(uuid_hint, []).append((entry, parsed, diags))

    for _entry, diags in v2_without_uuid:
        diagnostics.extend(diags)

    valid_by_uuid: Dict[str, board_lint.ParsedV2Message] = {}
    excluded_uuids: Set[str] = set()
    collapsed_alias_files: Set[str] = set()
    conflict_prevs_by_thread: Dict[str, Set[str]] = {}

    for msg_uuid in sorted(by_uuid):
        group = sorted(by_uuid[msg_uuid], key=lambda item: item[0].filename)
        if len(group) == 1:
            _entry, parsed, diags = group[0]
            if parsed is not None:
                valid_by_uuid[msg_uuid] = parsed
            else:
                diagnostics.extend(diags)
                excluded_uuids.add(msg_uuid)
            continue

        # Multiple files share msg_uuid
        all_parsed = all(item[1] is not None for item in group)
        if all_parsed:
            first_msg = group[0][1]
            identical = all(
                item[1].sha256 == first_msg.sha256
                and item[1].sender == first_msg.sender
                and item[1].recipient == first_msg.recipient
                for item in group[1:]
            )
        else:
            identical = False

        if all_parsed and identical:
            primary = group[0][1]
            valid_by_uuid[msg_uuid] = primary
            for _entry, alias_msg, _diags in group[1:]:
                collapsed_alias_files.add(alias_msg.filename)
                diagnostics.append(
                    board_lint.Diagnostic(
                        severity="warning",
                        code="RETRY_ALIAS",
                        file=alias_msg.filename,
                        message_id=msg_uuid,
                        thread_id=primary.thread,
                    )
                )
        else:
            # Differing bytes, differing route, or malformed copy -> REQUEST_CONFLICT
            excluded_uuids.add(msg_uuid)
            known_threads: Set[str] = set()
            for _entry, parsed, diags in group:
                if parsed is not None:
                    known_threads.add(parsed.thread)
                    conflict_prevs_by_thread.setdefault(parsed.thread, set()).add(parsed.prev)
                elif diags and diags[0].thread_id:
                    known_threads.add(diags[0].thread_id)
            if known_threads:
                for tid in sorted(known_threads):
                    _freeze_thread(tid, "REQUEST_CONFLICT")
            else:
                unknown_scope_freeze = True
            for entry, parsed, diags in group:
                tid = parsed.thread if parsed else (diags[0].thread_id if diags else None)
                diagnostics.append(
                    board_lint.Diagnostic(
                        severity="high",
                        code="REQUEST_CONFLICT",
                        file=entry.filename,
                        message_id=msg_uuid,
                        thread_id=tid,
                    )
                )

    # Number collisions across non-retry-collapsed files
    for num_str in sorted(number_groups):
        active_in_num = [
            fname for fname, _u in number_groups[num_str]
            if fname not in collapsed_alias_files
        ]
        if len(active_in_num) > 1:
            for fname in sorted(active_in_num):
                msg_id = None
                thr_id = None
                for u, m in valid_by_uuid.items():
                    if m.filename == fname:
                        msg_id = u
                        thr_id = m.thread
                        break
                diagnostics.append(
                    board_lint.Diagnostic(
                        severity="warning",
                        code="NUMBER_COLLISION",
                        file=fname,
                        message_id=msg_id,
                        thread_id=thr_id,
                    )
                )

    # --- 4. Index roots & successor candidates, order task supersedes topologically ---
    roots: Dict[str, board_lint.ParsedV2Message] = {}
    children_by_prev: Dict[str, List[board_lint.ParsedV2Message]] = {}
    non_roots_by_uuid: Dict[str, board_lint.ParsedV2Message] = {}

    for msg_uuid in sorted(valid_by_uuid):
        msg = valid_by_uuid[msg_uuid]
        if msg.prev == "none":
            roots[msg_uuid] = msg
        else:
            non_roots_by_uuid[msg_uuid] = msg
            children_by_prev.setdefault(msg.prev, []).append(msg)

    for prev_id in children_by_prev:
        children_by_prev[prev_id].sort(key=lambda m: m.request_id)

    # Detect supersedes cycles among task roots
    supersedes_cycle_roots: Set[str] = set()
    for rid in sorted(roots):
        if roots[rid].supersedes is None:
            continue
        seen = {rid}
        cur = roots[rid].supersedes
        while cur is not None and cur in roots:
            if cur in seen:
                supersedes_cycle_roots.add(rid)
                break
            seen.add(cur)
            cur = roots[cur].supersedes

    ordered_roots: List[str] = []
    visited_root_order: Set[str] = set()

    def _visit_root_order(rid: str) -> None:
        if rid in visited_root_order or rid in supersedes_cycle_roots:
            return
        sup = roots[rid].supersedes
        if sup is not None and sup in roots and sup not in supersedes_cycle_roots:
            _visit_root_order(sup)
        visited_root_order.add(rid)
        ordered_roots.append(rid)

    for rid in sorted(roots):
        _visit_root_order(rid)

    # --- 5. Deterministic replay per thread ---
    tasks_out: Dict[str, dict] = {}
    threads_out: Dict[str, dict] = {}
    pending_all: List[dict] = []
    accepted_uuids: Set[str] = set()
    rejected_uuids: Set[str] = set()
    fork_frozen_siblings: Set[str] = set()
    accepted_events_list: List[board_lint.ParsedV2Message] = []
    v2_records_view: List[dict] = []

    def _record_v2_view(msg: board_lint.ParsedV2Message, status: str, reason_code: Optional[str] = None) -> None:
        v2_records_view.append({
            "kind": "v2",
            "status": status,
            "reason_code": reason_code,
            "filename": msg.filename,
            "number": msg.number,
            "number_str": msg.number_str,
            "sha256": msg.sha256,
            "mtime_ns": msg.mtime_ns,
            "uuid": msg.request_id,
            "thread": msg.thread,
            "prev": msg.prev,
            "re": msg.re,
            "from": msg.sender,
            "to": msg.recipient,
            "type": msg.msg_type,
            "sent_at": msg.sent_at,
            "needs_human": "yes" if msg.needs_human else "no",
            "task": msg.task_label,
            "assignee": msg.assignee,
            "reviewer": msg.reviewer,
            "task_kind": msg.kind,
            "state": msg.state,
            "verdict": msg.verdict,
            "delivers": msg.delivers,
            "review_of": msg.review_of,
            "checklist": msg.checklist,
            "supersedes": msg.supersedes,
            "body": msg.body,
        })

    def _reject_msg(msg: board_lint.ParsedV2Message, code: str) -> None:
        rejected_uuids.add(msg.request_id)
        diagnostics.append(
            board_lint.Diagnostic(
                severity="error",
                code=code,
                file=msg.filename,
                message_id=msg.request_id,
                thread_id=msg.thread,
            )
        )
        _record_v2_view(msg, "rejected", code)

    # First handle supersedes cycle roots
    for rid in sorted(supersedes_cycle_roots):
        _reject_msg(roots[rid], "REFERENCE")

    # Seed frozen threads whose roots failed or are absent
    for tid in sorted(frozen_threads):
        if tid not in roots:
            threads_out[tid] = {
                "thread_id": tid,
                "root_uuid": None,
                "root_type": None,
                "task_label": None,
                "head_uuid": None,
                "event_count": 0,
                "paused": True,
                "pause_reason": frozen_threads[tid][0],
                "pending_id": None,
                "conflict_ids": [],
                "frozen": True,
                "freeze_reasons": list(frozen_threads[tid]),
                "terminal": False,
                "timeline": [],
            }

    for rid in ordered_roots:
        root = roots[rid]
        # Validate supersedes if present
        if root.supersedes is not None:
            if root.supersedes not in roots or root.supersedes in supersedes_cycle_roots:
                diagnostics.append(
                    board_lint.Diagnostic(
                        severity="warning",
                        code="DEFERRED_REF",
                        file=root.filename,
                        message_id=root.request_id,
                        thread_id=root.thread,
                    )
                )
                _record_v2_view(root, "deferred", "DEFERRED_REF")
                continue
            target_thr = threads_out.get(root.supersedes)
            target_task = tasks_out.get(root.supersedes)
            if (
                target_thr is None
                or target_task is None
                or not (target_thr["paused"] or target_thr["terminal"])
            ):
                _reject_msg(root, "REFERENCE")
                continue

        # Check if root's thread was frozen before walk (e.g., sibling conflict or immutability)
        pre_freeze = frozen_threads.get(rid, [])
        if pre_freeze:
            # Root itself was not excluded, so if thread frozen by external conflict/immutability,
            # still check: if root wasn't excluded, freeze thread at root or reject?
            # Note: if an event in thread was modified, freeze thread after root? Wait:
            # Spec: "Freeze known dependent threads and invalidate their live candidates."
            # Walk up until the missing/modified node? Since missing/modified node isn't in snapshot,
            # walking stops before it anyway, AND marking thread frozen=True blocks live dispatch!
            pass

        is_task = root.msg_type == "task"
        creator = root.sender
        assignee = root.assignee
        reviewer = root.reviewer
        task_kind = root.kind
        task_state = "open" if is_task else None
        participants = (
            ({creator, assignee} | ({reviewer} if reviewer else set()))
            if is_task
            else set()
        )

        accepted_chain: List[board_lint.ParsedV2Message] = [root]
        accepted_uuids.add(root.request_id)
        accepted_events_list.append(root)
        _record_v2_view(root, "accepted")

        accepted_types: Dict[str, str] = {root.request_id: root.msg_type}
        event_count = 1
        current_head = root.request_id
        paused = False
        pause_reason: Optional[str] = None
        pending_entry: Optional[dict] = None
        conflict_ids: List[str] = []

        active_delivery: Optional[dict] = None
        deliveries_history: List[dict] = []
        reviewed_delivery_ids: Set[str] = set()
        revise_count = 0

        def _open_pending(reason: str, trigger_uuid: str, trigger_sent_at: str) -> None:
            nonlocal pending_entry
            if pending_entry is not None:
                return
            pid = _make_pending_id(rid, reason, trigger_uuid)
            pending_entry = {
                "pending_id": pid,
                "thread_id": rid,
                "reason": reason,
                "trigger_uuid": trigger_uuid,
                "task_label": root.task_label,
                "triggered_at": trigger_sent_at,
                "open": pid not in dismissed_ids,
            }
            pending_all.append(pending_entry)

        if root.needs_human:
            paused = True
            pause_reason = "needs_human"
            _open_pending("needs_human", root.request_id, root.sent_at)

        while True:
            raw_candidates = children_by_prev.get(current_head, [])
            if not raw_candidates:
                break

            same_thread_candidates: List[board_lint.ParsedV2Message] = []
            for cand in raw_candidates:
                if cand.thread != rid:
                    _reject_msg(cand, "REFERENCE")
                else:
                    same_thread_candidates.append(cand)

            if not same_thread_candidates:
                break

            terminal_now = is_task and task_state in board_lint.TERMINAL_STATES
            frozen_here = (
                paused
                or terminal_now
                or (current_head in conflict_prevs_by_thread.get(rid, set()))
            )
            if frozen_here:
                for cand in same_thread_candidates:
                    _reject_msg(cand, "THREAD_CLOSED")
                break

            if pre_freeze and manifest_active:
                blocked_new = [c for c in same_thread_candidates if c.filename not in manifest_entries]
                kept_existing = [c for c in same_thread_candidates if c.filename in manifest_entries]
                for cand in blocked_new:
                    _reject_msg(cand, "THREAD_CLOSED")
                same_thread_candidates = kept_existing
                if not same_thread_candidates:
                    break

            # Evaluate each candidate against the same accepted prefix
            valid_candidates: List[board_lint.ParsedV2Message] = []
            for cand in same_thread_candidates:
                err_code = _validate_candidate(
                    cand=cand,
                    is_task=is_task,
                    creator=creator,
                    assignee=assignee,
                    reviewer=reviewer,
                    task_kind=task_kind,
                    task_state=task_state,
                    participants=participants,
                    accepted_types=accepted_types,
                    active_delivery=active_delivery,
                    reviewed_delivery_ids=reviewed_delivery_ids,
                )
                if err_code is not None:
                    _reject_msg(cand, err_code)
                else:
                    valid_candidates.append(cand)

            if not valid_candidates:
                break

            if len(valid_candidates) > 1:
                conflict_ids = sorted(c.request_id for c in valid_candidates)
                paused = True
                pause_reason = "thread_fork"
                _freeze_thread(rid, "THREAD_FORK")
                for c in valid_candidates:
                    fork_frozen_siblings.add(c.request_id)
                    _record_v2_view(c, "fork_frozen", "THREAD_FORK")
                diagnostics.append(
                    board_lint.Diagnostic(
                        severity="high",
                        code="THREAD_FORK",
                        file=valid_candidates[0].filename,
                        message_id=current_head,
                        thread_id=rid,
                    )
                )
                _open_pending("thread_fork", current_head, accepted_chain[-1].sent_at)
                break

            # Exactly one valid successor -> accept and apply transition
            chosen = valid_candidates[0]
            accepted_chain.append(chosen)
            accepted_uuids.add(chosen.request_id)
            accepted_types[chosen.request_id] = chosen.msg_type
            accepted_events_list.append(chosen)
            _record_v2_view(chosen, "accepted")
            event_count += 1
            current_head = chosen.request_id

            revise_paused = False
            if is_task:
                if chosen.msg_type == "state":
                    task_state = chosen.state
                elif chosen.msg_type == "delivery":
                    eff_delivers = chosen.delivers if chosen.delivers is not None else {}
                    active_delivery = {
                        "id": chosen.request_id,
                        "delivers": eff_delivers,
                        "submitted_at": chosen.sent_at,
                        "checklist_present": chosen.checklist is not None,
                    }
                    deliveries_history.append({
                        "id": chosen.request_id,
                        "delivers": eff_delivers,
                        "submitted_at": chosen.sent_at,
                        "checklist_present": chosen.checklist is not None,
                        "verdict": None,
                        "reviewed_at": None,
                    })
                    task_state = "in_review" if reviewer is not None else "done"
                elif chosen.msg_type == "review":
                    reviewed_delivery_ids.add(active_delivery["id"])
                    for drec in deliveries_history:
                        if drec["id"] == active_delivery["id"]:
                            drec["verdict"] = chosen.verdict
                            drec["reviewed_at"] = chosen.sent_at
                    active_delivery = None
                    if chosen.verdict == "approve":
                        task_state = "done"
                    elif chosen.verdict == "reject":
                        task_state = "rejected"
                    elif chosen.verdict == "revise":
                        task_state = "in_progress"
                        revise_count += 1
                        if revise_count >= 2:
                            revise_paused = True

            # Pause checks AFTER applying event (explicit needs_human still appears even if closing task)
            if chosen.needs_human:
                paused = True
                pause_reason = "needs_human"
                _open_pending("needs_human", chosen.request_id, chosen.sent_at)
            elif revise_paused:
                paused = True
                pause_reason = "second_revise"
                _open_pending("second_revise", chosen.request_id, chosen.sent_at)
            elif event_count >= board_lint.LOOP_BREAKER_LIMIT and not (
                is_task and task_state in board_lint.TERMINAL_STATES
            ):
                paused = True
                pause_reason = "loop_breaker_30"
                _open_pending("loop_breaker_30", chosen.request_id, chosen.sent_at)

        freeze_list = list(frozen_threads.get(rid, []))
        is_frozen = paused or bool(freeze_list)
        is_terminal = bool(is_task and task_state in board_lint.TERMINAL_STATES)

        threads_out[rid] = {
            "thread_id": rid,
            "root_uuid": root.request_id,
            "root_type": root.msg_type,
            "task_label": root.task_label,
            "head_uuid": current_head,
            "event_count": event_count,
            "paused": paused,
            "pause_reason": pause_reason,
            "pending_id": pending_entry["pending_id"] if pending_entry else None,
            "conflict_ids": conflict_ids,
            "frozen": is_frozen,
            "freeze_reasons": freeze_list if freeze_list else ([pause_reason] if pause_reason else []),
            "terminal": is_terminal,
            "timeline": [m.request_id for m in accepted_chain],
        }

        if is_task:
            latest_evt = accepted_chain[-1]
            obs_dt = _first_observed_dt(latest_evt.filename, manifest_entries, as_of_dt)
            effective_dt = min(latest_evt.sent_at_dt, obs_dt)
            is_stale = (not is_terminal) and ((as_of_dt - effective_dt) >= timedelta(hours=board_lint.STALE_HOURS))
            tasks_out[rid] = {
                "task_id": rid,
                "label": root.task_label,
                "kind": task_kind,
                "state": task_state,
                "roles": {
                    "creator": creator,
                    "assignee": assignee,
                    "reviewer": reviewer if reviewer is not None else "none",
                },
                "current_delivery": active_delivery,
                "deliveries": deliveries_history,
                "revise_count": revise_count,
                "last_event": {
                    "uuid": latest_evt.request_id,
                    "type": latest_evt.msg_type,
                    "from": latest_evt.sender,
                    "to": latest_evt.recipient,
                    "sent_at": latest_evt.sent_at,
                    "filename": latest_evt.filename,
                },
                "timeline": [m.request_id for m in accepted_chain],
                "event_count": event_count,
                "paused": paused,
                "pause_reason": pause_reason,
                "frozen": is_frozen,
                "stale": is_stale,
                "supersedes": root.supersedes,
            }

    # --- 6. Post-walk unvisited non-root events: cycles, cross-thread prev, deferred refs ---
    for msg_uuid in sorted(non_roots_by_uuid):
        if msg_uuid in accepted_uuids or msg_uuid in rejected_uuids or msg_uuid in fork_frozen_siblings:
            continue
        msg = non_roots_by_uuid[msg_uuid]
        # Check cycle or cross-thread predecessor
        seen = {msg_uuid}
        cur_prev = msg.prev
        is_cycle = False
        is_cross_thread = False
        while cur_prev in valid_by_uuid:
            prev_msg = valid_by_uuid[cur_prev]
            if prev_msg.thread != msg.thread:
                is_cross_thread = True
                break
            if cur_prev in seen:
                is_cycle = True
                break
            seen.add(cur_prev)
            if prev_msg.prev == "none":
                break
            cur_prev = prev_msg.prev

        if is_cycle or is_cross_thread:
            _reject_msg(msg, "REFERENCE")
        else:
            diagnostics.append(
                board_lint.Diagnostic(
                    severity="warning",
                    code="DEFERRED_REF",
                    file=msg.filename,
                    message_id=msg.request_id,
                    thread_id=msg.thread,
                )
            )
            _record_v2_view(msg, "deferred", "DEFERRED_REF")

    # --- 7. Per-recipient inboxes (accepted v2 events only; 'all' expands locally) ---
    inbox_recipients = get_inbox_recipients()
    inboxes: Dict[str, List[dict]] = {agent: [] for agent in inbox_recipients}
    accepted_sorted = sorted(
        accepted_events_list,
        key=lambda m: (m.sent_at, m.number, m.filename, m.request_id),
    )
    for msg in accepted_sorted:
        thr_info = threads_out[msg.thread]
        task_info = tasks_out.get(msg.thread)
        obs_dt = _first_observed_dt(msg.filename, manifest_entries, as_of_dt)
        entry_base = {
            "uuid": msg.request_id,
            "thread_id": msg.thread,
            "filename": msg.filename,
            "number": msg.number,
            "from": msg.sender,
            "to": msg.recipient,
            "type": msg.msg_type,
            "sent_at": msg.sent_at,
            "first_observed_at": board_lint.format_utc_rfc3339(obs_dt),
            "needs_human": msg.needs_human,
            "task_label": task_info["label"] if task_info else thr_info["task_label"],
            "task_state": task_info["state"] if task_info else None,
            "thread_paused": thr_info["paused"],
            "thread_frozen": thr_info["frozen"],
            "thread_terminal": thr_info["terminal"],
            "is_broadcast": msg.recipient == "all",
            "is_baseline": msg.request_id in known_baseline_uuids,
        }
        if msg.recipient == "all":
            for agent in inbox_recipients:
                if agent != msg.sender:
                    inboxes[agent].append(dict(entry_base))
        elif msg.recipient in inboxes:
            inboxes[msg.recipient].append(dict(entry_base))

    # --- 8. Diagnostics, exit code, pending, and metadata-only mail digest ---
    diagnostics.sort(key=_diag_sort_key)
    diag_dicts = [d.to_dict() for d in diagnostics]
    counts = {
        "high": sum(1 for d in diagnostics if d.severity == "high"),
        "error": sum(1 for d in diagnostics if d.severity == "error"),
        "warning": sum(1 for d in diagnostics if d.severity == "warning"),
    }
    if runtime_corrupt or manifest_corrupt:
        exit_code = 2
    elif counts["high"] > 0 or counts["error"] > 0:
        exit_code = 1
    else:
        exit_code = 0

    lint_report = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "as_of": as_of_str,
        "exit_code": exit_code,
        "counts": counts,
        "diagnostics": diag_dicts,
    }

    open_pending = sorted(
        [p for p in pending_all if p["open"]],
        key=lambda p: (p["thread_id"], p["pending_id"]),
    )
    resolved_pending = sorted(
        [p for p in pending_all if not p["open"]],
        key=lambda p: (p["thread_id"], p["pending_id"]),
    )

    high_incidents = []
    for d in diagnostics:
        if d.severity == "high":
            high_incidents.append({
                "incident_id": _make_incident_id(d),
                "code": d.code,
                "message_id": d.message_id,
                "thread_id": d.thread_id,
            })

    mail_digest = build_mail_digest_payload(
        snapshot_id=snapshot_id,
        as_of=as_of_str,
        open_pending=open_pending,
        high_incidents=high_incidents,
    )

    health = {
        "healthy": True,
        "stale": False,
        "scan_healthy": True,
        "runtime_corrupt": bool(runtime_corrupt or manifest_corrupt),
        "live_dispatch_suppressed": unknown_scope_freeze,
        "frozen_thread_count": len(frozen_threads),
        "high_incident_count": counts["high"],
        "stale_task_count": sum(1 for t in tasks_out.values() if t["stale"]),
    }

    state_doc = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "as_of": as_of_str,
        "health": health,
        "counts": {
            "legacy_messages": len(legacy_messages),
            "v2_accepted": len(accepted_events_list),
            "v2_rejected": len(rejected_uuids),
            "tasks": len(tasks_out),
            "threads": len(threads_out),
            "pending_open": len(open_pending),
            "pending_resolved": len(resolved_pending),
            "lint_high": counts["high"],
            "lint_error": counts["error"],
            "lint_warning": counts["warning"],
        },
        "tasks": {k: tasks_out[k] for k in sorted(tasks_out)},
        "threads": {k: threads_out[k] for k in sorted(threads_out)},
        "pending": open_pending,
        "resolved_pending": resolved_pending,
        "inboxes": inboxes,
    }

    v2_records_view.sort(key=lambda r: (r["sent_at"], r["number"], r["filename"], r["uuid"]))

    return {
        "snapshot_id": snapshot_id,
        "as_of": as_of_str,
        "health": health,
        "lint": lint_report,
        "state": state_doc,
        "mail_pending": mail_digest,
        "legacy_messages": legacy_messages,
        "v2_records": v2_records_view,
    }


def _validate_candidate(
    cand: board_lint.ParsedV2Message,
    is_task: bool,
    creator: str,
    assignee: Optional[str],
    reviewer: Optional[str],
    task_kind: Optional[str],
    task_state: Optional[str],
    participants: Set[str],
    accepted_types: Dict[str, str],
    active_delivery: Optional[dict],
    reviewed_delivery_ids: Set[str],
) -> Optional[str]:
    """Validate one candidate event against the current accepted thread prefix."""
    if cand.re is not None and cand.re not in accepted_types:
        return "REFERENCE"
    if cand.msg_type == "answer":
        if cand.re is None or accepted_types.get(cand.re) != "question":
            return "REFERENCE"

    if cand.msg_type == "task":
        return "TRANSITION"

    if cand.msg_type == "state":
        if not is_task:
            return "TRANSITION"
        if cand.sender != assignee or cand.recipient != creator:
            return "ROLE"
        allowed = {
            ("open", "claimed"),
            ("claimed", "in_progress"),
            ("in_progress", "blocked"),
            ("blocked", "in_progress"),
        }
        if (task_state, cand.state) not in allowed:
            return "TRANSITION"
        return None

    if cand.msg_type == "delivery":
        if not is_task:
            return "TRANSITION"
        if task_state != "in_progress":
            return "TRANSITION"
        expected_to = reviewer if reviewer is not None else creator
        if cand.sender != assignee or cand.recipient != expected_to:
            return "ROLE"
        if task_kind == "code":
            if cand.delivers is None or len(cand.delivers) == 0:
                return "DELIVERY"
        if assignee == "dsh" and task_kind == "code" and cand.checklist is None:
            return "CHECKLIST"
        return None

    if cand.msg_type == "review":
        if not is_task:
            return "TRANSITION"
        if task_state != "in_review":
            return "TRANSITION"
        if reviewer is None or cand.sender != reviewer or cand.recipient != assignee:
            return "ROLE"
        if active_delivery is None or active_delivery["id"] in reviewed_delivery_ids:
            return "STALE_REVIEW"
        if (
            cand.review_of["id"] != active_delivery["id"]
            or cand.review_of["delivers"] != active_delivery["delivers"]
        ):
            return "STALE_REVIEW"
        return None

    if cand.msg_type in ("info", "question", "answer"):
        if is_task:
            if cand.sender not in participants:
                return "ROLE"
            if cand.recipient not in participants:
                if not (cand.recipient == board_lint.HUMAN_ROLE and cand.needs_human):
                    return "ROLE"
        return None

    return "FORMAT"


# --- Metadata-only mail digest & Markdown projections ---

FIXED_MAIL_SUBJECT = "BoardHub attention needed"


def build_mail_digest_payload(
    snapshot_id: str,
    as_of: str,
    open_pending: List[dict],
    high_incidents: List[dict],
    wake_attention_items: Optional[List[dict]] = None,
) -> dict:
    """Build strictly metadata-only mail_pending.json payload (no body/task labels/paths)."""
    items: List[dict] = []
    seen_ids: Set[str] = set()

    for p in open_pending:
        item_id = p["pending_id"]
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        items.append({
            "item_id": item_id,
            "kind": "pending",
            "reason_code": p["reason"],
            "message_id": p["trigger_uuid"],
            "thread_id": p["thread_id"],
        })

    for inc in high_incidents:
        item_id = inc["incident_id"]
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        items.append({
            "item_id": item_id,
            "kind": "high_incident",
            "reason_code": inc["code"],
            "message_id": inc["message_id"],
            "thread_id": inc["thread_id"],
        })

    for w in (wake_attention_items or []):
        item_id = w["item_id"]
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        items.append({
            "item_id": item_id,
            "kind": "wake_attention",
            "reason_code": w["reason_code"],
            "message_id": w["message_id"],
            "thread_id": w.get("thread_id"),
        })

    items.sort(key=lambda r: (r["kind"], r["reason_code"], r["item_id"]))
    counts = {
        "pending_open": sum(1 for i in items if i["kind"] == "pending"),
        "high_incidents": sum(1 for i in items if i["kind"] == "high_incident"),
        "wake_attention": sum(1 for i in items if i["kind"] == "wake_attention"),
        "total": len(items),
    }

    lines = [
        f"pending_open: {counts['pending_open']}",
        f"high_incidents: {counts['high_incidents']}",
        f"wake_attention: {counts['wake_attention']}",
        f"total: {counts['total']}",
    ]
    for item in items:
        lines.append(
            f"- id={item['item_id']} kind={item['kind']} reason={item['reason_code']} "
            f"message_id={item['message_id'] or 'none'} thread_id={item['thread_id'] or 'none'}"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "as_of": as_of,
        "counts": counts,
        "fixed_subject": FIXED_MAIL_SUBJECT,
        "generated_body": "\n".join(lines) + "\n",
        "items": items,
    }


def render_status_markdown(result: dict) -> str:
    """Render _hub/STATUS.md from projection result with escaped metadata."""
    state = result["state"]
    lint = result["lint"]
    health = result["health"]
    lines = [
        f"# BoardHub Status",
        f"",
        f"- snapshot_id: `{state['snapshot_id']}`",
        f"- as_of: `{state['as_of']}`",
        f"- healthy: `{health['healthy']}` | stale: `{health['stale']}` | live_dispatch_suppressed: `{health['live_dispatch_suppressed']}`",
        f"- v2 accepted: `{state['counts']['v2_accepted']}` | legacy: `{state['counts']['legacy_messages']}` | tasks: `{state['counts']['tasks']}`",
        f"- lint: high=`{lint['counts']['high']}` error=`{lint['counts']['error']}` warning=`{lint['counts']['warning']}`",
        "",
        "## Pending Human Decisions",
    ]
    if not state["pending"]:
        lines.append("- None")
    else:
        for item in state["pending"]:
            label = board_lint.escape_safe_text(item["task_label"] or "-")
            lines.append(
                f"- `{item['pending_id']}` | thread=`{item['thread_id']}` | "
                f"reason=`{item['reason']}` | trigger=`{item['trigger_uuid']}` | label=`{label}`"
            )

    lines.extend(["", "## Tasks"])
    if not state["tasks"]:
        lines.append("- None")
    else:
        for tid in sorted(state["tasks"]):
            t = state["tasks"][tid]
            label = board_lint.escape_safe_text(t["label"] or "-")
            flags = []
            if t["stale"]:
                flags.append("STALE>24h")
            if t["paused"]:
                flags.append(f"PAUSED:{t['pause_reason']}")
            flag_str = " [" + ", ".join(flags) + "]" if flags else ""
            lines.append(
                f"- `{tid}` ({label}) state=`{t['state']}` "
                f"assignee=`{t['roles']['assignee']}` reviewer=`{t['roles']['reviewer']}` "
                f"events=`{t['event_count']}/{board_lint.LOOP_BREAKER_LIMIT}`{flag_str}"
            )

    paused_threads = [thr for thr in state["threads"].values() if thr["paused"] or thr["frozen"]]
    lines.extend(["", "## Paused or Frozen Threads"])
    if not paused_threads:
        lines.append("- None")
    else:
        for thr in sorted(paused_threads, key=lambda x: x["thread_id"]):
            lines.append(
                f"- `{thr['thread_id']}` reason=`{thr['pause_reason'] or ','.join(thr['freeze_reasons'])}` "
                f"head=`{thr['head_uuid'] or 'none'}` events=`{thr['event_count']}`"
            )

    lines.extend(["", "## Lint Diagnostics"])
    if not lint["diagnostics"]:
        lines.append("- None")
    else:
        for d in lint["diagnostics"][:100]:
            fname = board_lint.escape_safe_text(d["file"])
            lines.append(
                f"- [{d['severity']}] `{d['code']}` file=`{fname}` "
                f"msg=`{d['message_id'] or 'none'}` thread=`{d['thread_id'] or 'none'}`"
            )
    return "\n".join(lines) + "\n"


def render_inbox_markdown(agent: str, result: dict) -> str:
    """Render _hub/inbox_<agent>.md marked with snapshot_id and escaped metadata."""
    state = result["state"]
    items = state["inboxes"].get(agent, [])
    lines = [
        f"# Inbox: {agent}",
        "",
        f"- snapshot_id: `{state['snapshot_id']}`",
        f"- as_of: `{state['as_of']}`",
        f"- count: `{len(items)}`",
        "",
    ]
    if not items:
        lines.append("No v2 messages.")
    else:
        for item in items:
            label = board_lint.escape_safe_text(item["task_label"] or "-")
            fname = board_lint.escape_safe_text(item["filename"])
            flags = []
            if item["is_broadcast"]:
                flags.append("broadcast")
            if item["needs_human"]:
                flags.append("needs_human")
            if item["thread_paused"]:
                flags.append("paused")
            if item["thread_terminal"]:
                flags.append("terminal")
            if item["is_baseline"]:
                flags.append("baseline")
            flag_str = f" flags=[{','.join(flags)}]" if flags else ""
            lines.append(
                f"- `{item['sent_at']}` `{item['from']}` -> `{item['to']}` "
                f"type=`{item['type']}` uuid=`{item['uuid']}` thread=`{item['thread_id']}` "
                f"file=`{fname}` label=`{label}`{flag_str}"
            )
    return "\n".join(lines) + "\n"


# --- Atomic _hub projection publisher (state.json published LAST) ---

def _ensure_safe_hub_dir(board_dir: Path) -> Path:
    board_info = board_dir.lstat()
    if stat.S_ISLNK(board_info.st_mode) or not stat.S_ISDIR(board_info.st_mode):
        raise PublishError("Board directory is not a regular directory.")
    hub_dir = board_dir / "_hub"
    try:
        hinfo = hub_dir.lstat()
    except FileNotFoundError:
        os.mkdir(hub_dir, 0o700)
        return hub_dir
    if stat.S_ISLNK(hinfo.st_mode) or not stat.S_ISDIR(hinfo.st_mode):
        raise PublishError("_hub path is a symlink or non-directory.")
    return hub_dir


def _sync_dir_best_effort(dir_path: Path) -> None:
    """Sync directory where supported; ignore ENOTSUP/EINVAL/EBADF on exFAT, raise real I/O errors."""
    try:
        dfd = os.open(dir_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise DurabilityError(f"Failed to open directory for sync: {exc}") from exc
    try:
        try:
            os.fsync(dfd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF, errno.EACCES):
                raise DurabilityError(f"Directory fsync failed: {exc}") from exc
    finally:
        os.close(dfd)


def _atomic_write_hub_file(hub_dir: Path, name: str, content: bytes) -> None:
    tmp_name = f".tmp_{name}_{os.getpid()}_{secrets.token_hex(6)}"
    tmp_path = hub_dir / tmp_name
    final_path = hub_dir / name
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise DurabilityError(f"Cannot create temp projection file {name}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError as exc:
                raise DurabilityError(f"Projection fsync failed for {name}: {exc}") from exc
        os.replace(tmp_path, final_path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def publish_hub_projections(
    board_dir: Path,
    result: dict,
    healthy: bool = True,
    health_record: Optional[dict] = None,
) -> None:
    """Publish _hub projections atomically with state.json written LAST; preserve good state if unhealthy."""
    board_path = Path(board_dir)
    hub_dir = _ensure_safe_hub_dir(board_path)

    if not healthy:
        if health_record is not None:
            hbytes = (json.dumps(health_record, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
            _atomic_write_hub_file(hub_dir, "HEALTH.json", hbytes)
            _sync_dir_best_effort(hub_dir)
        return

    # Write non-state views first
    lint_bytes = (json.dumps(result["lint"], indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_hub_file(hub_dir, "lint.json", lint_bytes)

    status_bytes = render_status_markdown(result).encode("utf-8")
    _atomic_write_hub_file(hub_dir, "STATUS.md", status_bytes)

    for agent in get_inbox_recipients():
        inbox_bytes = render_inbox_markdown(agent, result).encode("utf-8")
        _atomic_write_hub_file(hub_dir, f"inbox_{agent}.md", inbox_bytes)

    if health_record is not None:
        hbytes = (json.dumps(health_record, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        _atomic_write_hub_file(hub_dir, "HEALTH.json", hbytes)

    # Publish state.json LAST so readers observing updated state.json see matching snapshot IDs everywhere
    state_bytes = (json.dumps(result["state"], indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_hub_file(hub_dir, "state.json", state_bytes)
    _sync_dir_best_effort(hub_dir)


# --- Safe concurrent v2 message publisher ---

def _get_publish_lock(board_dir: Path, message_uuid: str) -> threading.Lock:
    key = (str(board_dir.resolve()), message_uuid)
    with _PUBLISH_LOCKS_GUARD:
        lock = _PUBLISH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PUBLISH_LOCKS[key] = lock
        return lock


def publish_message(
    board_dir: Path,
    number: int,
    from_agent: str,
    to_agent: str,
    message_uuid: str,
    content_bytes: bytes,
    sync_dir: bool = True,
    _fsync_file_fn=os.fsync,
    _fsync_dir_fn=None,
) -> dict:
    """Atomically publish a v2 board message with per-UUID retry serialization & conflict detection."""
    if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= 9999:
        raise PublishError("number must be an integer 0..9999.")
    if from_agent not in board_lint.VALID_SENDERS:
        raise PublishError("Invalid sender actor.")
    if to_agent not in board_lint.VALID_RECIPIENTS:
        raise PublishError("Invalid recipient actor.")
    if not isinstance(message_uuid, str) or not board_lint.UUID_V4_RE.fullmatch(message_uuid):
        raise PublishError("message_uuid must be a canonical lowercase UUIDv4.")
    if not isinstance(content_bytes, (bytes, bytearray)):
        raise PublishError("content_bytes must be bytes.")
    raw = bytes(content_bytes)

    board_path = Path(board_dir)
    final_name = f"{number:04d}_{from_agent}_to_{to_agent}--{message_uuid}.md"
    dummy_entry = board_lint.SnapshotEntry(
        filename=final_name,
        sha256=hashlib.sha256(raw).hexdigest(),
        raw_bytes=raw,
        size=len(raw),
        mtime_ns=0,
    )
    parsed, diags = board_lint.parse_v2_message(dummy_entry)
    if parsed is None:
        code = diags[0].code if diags else "FORMAT"
        raise PublishError(f"Message failed v2 validation ({code}).")

    uuid_lock = _get_publish_lock(board_path, message_uuid)
    with uuid_lock:
        binfo = board_path.lstat()
        if stat.S_ISLNK(binfo.st_mode) or not stat.S_ISDIR(binfo.st_mode):
            raise PublishError("Board directory is unsafe or missing.")

        # Check existing files carrying this UUID before creating temp file
        existing = sorted(
            name for name in os.listdir(board_path)
            if not name.startswith(".") and name.endswith(f"--{message_uuid}.md")
        )
        if existing:
            return _verify_existing_publication(board_path, existing, raw, from_agent, to_agent, message_uuid)

        tmp_name = f".tmp_{message_uuid}_{os.getpid()}_{secrets.token_hex(6)}.md"
        tmp_path = board_path / tmp_name
        final_path = board_path / final_name

        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise DurabilityError(f"Failed creating temp message file: {exc}") from exc

        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                try:
                    _fsync_file_fn(handle.fileno())
                except OSError as exc:
                    raise DurabilityError(f"File fsync failed: {exc}") from exc

            # Re-check existing UUID files before rename so we never replace or renumber
            existing_now = sorted(
                name for name in os.listdir(board_path)
                if not name.startswith(".") and name.endswith(f"--{message_uuid}.md")
            )
            if existing_now or final_path.exists() or final_path.is_symlink():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                targets = existing_now or [final_name]
                return _verify_existing_publication(board_path, targets, raw, from_agent, to_agent, message_uuid)

            os.replace(tmp_path, final_path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

        if sync_dir:
            if _fsync_dir_fn is not None:
                _fsync_dir_fn(board_path)
            else:
                _sync_dir_best_effort(board_path)

        return {
            "status": "published",
            "filename": final_name,
            "uuid": message_uuid,
            "sha256": dummy_entry.sha256,
        }


def _verify_existing_publication(
    board_path: Path,
    existing_names: List[str],
    raw: bytes,
    from_agent: str,
    to_agent: str,
    message_uuid: str,
) -> dict:
    if len(existing_names) != 1:
        raise RequestConflictError(f"Multiple copies already exist for UUID {message_uuid}.")
    ename = existing_names[0]
    epath = board_path / ename
    einfo = epath.lstat()
    if stat.S_ISLNK(einfo.st_mode) or not stat.S_ISREG(einfo.st_mode):
        raise RequestConflictError(f"Existing entry for {message_uuid} is unsafe.")
    fd = os.open(epath, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        existing_bytes = os.read(fd, board_lint.MAX_V2_FILE_BYTES + 1)
    finally:
        os.close(fd)
    m_existing = board_lint.V2_FILENAME_RE.fullmatch(ename)
    if (
        m_existing is None
        or existing_bytes != raw
        or m_existing[2] != from_agent
        or m_existing[3] != to_agent
        or m_existing[4] != message_uuid
    ):
        raise RequestConflictError(
            f"UUID {message_uuid} already exists with differing bytes or route ({ename})."
        )
    return {
        "status": "idempotent_ok",
        "filename": ename,
        "uuid": message_uuid,
        "sha256": hashlib.sha256(existing_bytes).hexdigest(),
    }


# --- CLI ---

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Rebuild BoardHub state from board snapshot.")
    parser.add_argument(
        "--board",
        type=Path,
        default=None,
        help="Path to board directory (env: BOARDHUB_BOARD_DIR, default: ~/boardhub/board).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional JSON config file (env: BOARDHUB_CONFIG).",
    )
    parser.add_argument(
        "--human",
        default=None,
        help="Human approver role name (env: BOARDHUB_HUMAN, default: owner).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Optional runtime manifest JSON path.",
    )
    parser.add_argument(
        "--housekeeping",
        type=Path,
        default=None,
        help="Optional runtime housekeeping receipts JSON path.",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Optional UTC RFC 3339 timestamp.",
    )
    parser.add_argument(
        "--publish-hub",
        action="store_true",
        help="Publish state.json, lint.json, STATUS.md, and inboxes to <board>/_hub.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print rebuilt state JSON to stdout.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        board_dir, _runtime_dir, _human_role = board_lint.resolve_cli_settings(args)
    except (board_lint.SecurityError, board_lint.FormatError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.as_of is not None:
        as_of = board_lint.parse_utc_rfc3339(args.as_of)
        if as_of is None:
            print("Invalid --as-of timestamp.", file=sys.stderr)
            return 2
    else:
        as_of = datetime.now(timezone.utc)

    runtime_corrupt = False
    manifest = {}
    if args.manifest is not None:
        loaded_m, m_corrupt = load_manifest(args.manifest)
        if m_corrupt:
            runtime_corrupt = True
        elif loaded_m is not None:
            manifest = loaded_m

    housekeeping_ids: Set[str] = set()
    if args.housekeeping is not None:
        hk_ids, _hk_obj, hk_corrupt = load_housekeeping_receipts(args.housekeeping)
        if hk_corrupt:
            runtime_corrupt = True
        else:
            housekeeping_ids = hk_ids

    snapshot = board_lint.scan_board(board_dir)
    result = rebuild(
        snapshot,
        manifest,
        as_of,
        housekeeping_ids=housekeeping_ids,
        runtime_corrupt=runtime_corrupt,
    )

    if args.publish_hub:
        publish_hub_projections(
            board_dir,
            result,
            healthy=result["health"]["healthy"],
            health_record=result["health"],
        )

    if args.json:
        print(json.dumps(result["state"], indent=2, sort_keys=True, ensure_ascii=False))
    else:
        c = result["state"]["counts"]
        print(
            f"board_state: snapshot={result['snapshot_id']} healthy={result['health']['healthy']} "
            f"v2={c['v2_accepted']} legacy={c['legacy_messages']} tasks={c['tasks']} "
            f"pending={c['pending_open']} lint_exit={result['lint']['exit_code']}"
        )
    return 0 if result["health"]["healthy"] and not runtime_corrupt else 2


if __name__ == "__main__":
    sys.exit(main())
