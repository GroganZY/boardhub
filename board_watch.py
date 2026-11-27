#!/usr/bin/env python3
"""BoardHub polling watcher (--once / 20s loop), APFS runtime manager, and LaunchAgent target.

Python 3.8+ standard library only.
"""

import argparse
import contextlib
from datetime import datetime, timezone
import errno
import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import syslog
import time
from typing import Callable, Optional, Set, Tuple
import uuid

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

board_state = board_lint.load_sibling_module("board_state")
wake_routes = board_lint.load_sibling_module("wake_routes")


DEFAULT_BOARD_DIR = board_lint.default_board_dir()
DEFAULT_RUNTIME_DIR = board_lint.default_runtime_dir()
DEFAULT_INTERVAL_SECONDS = 20

MANIFEST_NAME = "manifest.json"
BASELINE_NAME = "baseline.json"
HOUSEKEEPING_NAME = "housekeeping.json"
ROUTES_NAME = "routes.json"
WAKE_LEDGER_NAME = "wake_ledger.json"
MAIL_LEDGER_NAME = "mail_ledger.json"
MAIL_PENDING_NAME = "mail_pending.json"
HEALTH_NAME = "health.json"
LAST_PUBLISHED_NAME = "last_published.json"
LOCK_NAME = "watch.lock"
LOG_NAME = "watch.log"
BOARD_ID_NAME = ".boardhub_id"

LOG = logging.getLogger("boardhub_watch")


class WatcherError(Exception):
    """Raised when watcher environment, lock, or APFS runtime checks fail."""


def _syslog_error(message: str) -> None:
    with contextlib.suppress(OSError):
        syslog.openlog("boardhub-watch")
        syslog.syslog(syslog.LOG_ERR, message)


def ensure_non_root() -> None:
    if os.getuid() == 0 or os.geteuid() == 0:
        raise WatcherError("Refusing to run BoardHub watcher as root.")


def verify_apfs_residence(path: Path) -> None:
    """Verify runtime directory (default or custom) resides on APFS (macOS)."""
    if sys.platform != "darwin":
        return
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        df_res = subprocess.run(
            ["/bin/df", "-P", str(probe)],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        mount_res = subprocess.run(
            ["/sbin/mount"],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        lines = [ln for ln in df_res.stdout.splitlines() if ln.strip()]
        if df_res.returncode != 0 or len(lines) < 2 or mount_res.returncode != 0:
            raise WatcherError("Unable to verify APFS filesystem for runtime directory.")
        dev = lines[1].split()[0]
        is_apfs = any(
            ln.startswith(dev + " ") and ("(apfs," in ln or "(apfs)" in ln)
            for ln in mount_res.stdout.splitlines()
        )
        if not is_apfs:
            raise WatcherError("Runtime directory must reside on APFS.")
    except subprocess.TimeoutExpired:
        raise WatcherError("Filesystem verification timed out.") from None


def open_validated_log_stream(runtime_dir: Path):
    """Verify runtime_dir and open watch.log with O_NOFOLLOW, 0600, and ACL checks."""
    runtime_dir = board_lint.verify_runtime_dir(runtime_dir, expected_mode=0o700, create=True)
    log_path = runtime_dir / LOG_NAME
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise WatcherError(f"Cannot safely open {LOG_NAME}.") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WatcherError(f"Unexpected file type for {LOG_NAME}.")
        if info.st_uid != os.getuid():
            raise WatcherError(f"Unsafe ownership on {LOG_NAME}.")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise WatcherError(f"Unsafe permissions on {LOG_NAME}.")
        board_lint.check_no_allow_acl_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "a", encoding="utf-8")


@contextlib.contextmanager
def acquire_watcher_lock(runtime_dir: Path):
    """Acquire nonblocking APFS lock so only one watcher runs at a time."""
    runtime_dir = board_lint.verify_runtime_dir(runtime_dir, expected_mode=0o700, create=True)
    lock_path = runtime_dir / LOCK_NAME
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise WatcherError(f"Cannot open watcher lock: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise WatcherError("Unsafe watcher lock file attributes.")
        board_lint.check_no_allow_acl_fd(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                raise WatcherError("Another BoardHub watcher instance is already running.") from None
            raise WatcherError("Failed acquiring watcher lock.") from exc
        yield lock_path
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_baseline(path: Path) -> Tuple[Optional[Set[str]], bool]:
    try:
        obj = board_lint.read_private_json_file(path)
    except (board_lint.SecurityError, board_lint.FormatError, OSError):
        return None, True
    if obj is None:
        return None, False
    if obj.get("version") != board_state.SCHEMA_VERSION or not isinstance(obj.get("baseline_uuids"), list):
        return None, True
    uuids = set()
    for item in obj["baseline_uuids"]:
        if not isinstance(item, str) or not board_lint.UUID_V4_RE.fullmatch(item):
            return None, True
        uuids.add(item)
    return uuids, False


def _save_baseline(path: Path, baseline_uuids: Set[str], as_of: datetime) -> None:
    obj = {
        "version": board_state.SCHEMA_VERSION,
        "bootstrapped_at": board_lint.format_utc_rfc3339(as_of),
        "baseline_uuids": sorted(baseline_uuids),
    }
    payload = (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    board_lint.atomic_write_private_file(path, payload, mode=0o600)


def _write_apfs_json(path: Path, obj: dict) -> None:
    payload = (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    board_lint.atomic_write_private_file(path, payload, mode=0o600)


def _read_last_published(path: Path) -> Tuple[Optional[dict], bool]:
    try:
        obj = board_lint.read_private_json_file(path)
    except (board_lint.SecurityError, board_lint.FormatError, OSError):
        return None, True
    return obj, False


def _extract_accepted_uuids(result: dict) -> Set[str]:
    return {r["uuid"] for r in result.get("v2_records", []) if r.get("status") == "accepted"}


def _update_manifest_with_new_entries(
    snapshot: board_lint.BoardSnapshot,
    manifest: dict,
    as_of: datetime,
) -> Tuple[dict, bool]:
    """Append newly observed basenames to manifest without modifying existing entries."""
    existing_entries = manifest.get("entries", {})
    new_names = [fn for fn in sorted(snapshot.entries) if fn not in existing_entries]
    if not new_names:
        return manifest, False
    built = board_state.build_manifest_from_snapshot(snapshot, as_of, existing_manifest=manifest, reconcile=False)
    merged_entries = dict(existing_entries)
    for fn in new_names:
        merged_entries[fn] = built["entries"][fn]
    updated = dict(manifest)
    updated["entries"] = merged_entries
    return updated, True


def _read_board_id_fd(dir_fd: int) -> Tuple[Optional[str], bool]:
    """Read canonical UUIDv4 from .boardhub_id inside dir_fd. Returns (uuid_or_none, unsafe_or_corrupt)."""
    try:
        fd = os.open(BOARD_ID_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 128:
            return None, True
        raw = os.read(fd, 129)
    except OSError:
        return None, True
    finally:
        os.close(fd)
    if len(raw) > 128:
        return None, True
    try:
        val = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None, True
    if not board_lint.UUID_V4_RE.fullmatch(val):
        return None, True
    return val, False


def _ensure_board_id(board_dir: Path, expected_root_id: Optional[Tuple[int, int]] = None) -> str:
    """Read or atomically establish .boardhub_id inside pinned board_dir."""
    board_path = Path(board_dir)
    try:
        dfd = os.open(board_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise WatcherError(f"Cannot safely open board directory for identity check: {exc}") from exc
    try:
        finfo = os.fstat(dfd)
        if not stat.S_ISDIR(finfo.st_mode):
            raise WatcherError("Board directory is not a directory.")
        if expected_root_id is not None and (finfo.st_dev, finfo.st_ino) != expected_root_id:
            raise WatcherError("Board root directory identity changed while establishing board_id.")
        existing, unsafe = _read_board_id_fd(dfd)
        if unsafe:
            raise WatcherError("Board identity marker .boardhub_id is unsafe or corrupt.")
        if existing is not None:
            return existing
        new_id = str(uuid.uuid4())
        tmp_name = f".tmp_{BOARD_ID_NAME}_{os.getpid()}_{secrets.token_hex(6)}"
        tfd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(tfd, "wb") as handle:
                handle.write((new_id + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            existing_now, unsafe_now = _read_board_id_fd(dfd)
            if unsafe_now:
                raise WatcherError("Board identity marker .boardhub_id is unsafe or corrupt.")
            if existing_now is not None:
                return existing_now
            os.replace(tmp_name, BOARD_ID_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name, dir_fd=dfd)
        return new_id
    finally:
        os.close(dfd)


def _verify_board_root_pinned(
    board_dir: Path,
    expected_dev: Optional[int],
    expected_ino: Optional[int],
    expected_board_id: Optional[str] = None,
) -> bool:
    if expected_dev is None or expected_ino is None:
        return False
    board_path = Path(board_dir)
    try:
        linfo = board_path.lstat()
        if stat.S_ISLNK(linfo.st_mode) or not stat.S_ISDIR(linfo.st_mode):
            return False
        dir_fd = os.open(board_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            finfo = os.fstat(dir_fd)
            if expected_board_id is not None:
                bid, unsafe = _read_board_id_fd(dir_fd)
                if unsafe or bid != expected_board_id:
                    return False
        finally:
            os.close(dir_fd)
    except OSError:
        return False
    return (
        stat.S_ISDIR(finfo.st_mode)
        and (linfo.st_dev, linfo.st_ino) == (expected_dev, expected_ino)
        and (finfo.st_dev, finfo.st_ino) == (expected_dev, expected_ino)
    )


def bootstrap_runtime(
    board_dir: Path,
    runtime_dir: Path,
    as_of: Optional[datetime] = None,
    _after_wake_save_hook: Optional[Callable[[], None]] = None,
) -> dict:
    """Explicitly bootstrap APFS manifest, baseline (inbox-only), route baselines, and _hub views."""
    ensure_non_root()
    runtime_dir = board_lint.verify_runtime_dir(runtime_dir, expected_mode=0o700, create=True)
    already_init = wake_routes.is_wake_ledger_initialized(runtime_dir)
    now = as_of or datetime.now(timezone.utc)

    # 1. Scan board and validate all existing runtime configs/ledgers BEFORE writing anything
    snapshot = board_lint.scan_board(board_dir)
    if not snapshot.healthy:
        health_rec = {
            "healthy": False,
            "stale": True,
            "reason": snapshot.scan_error or "scan_unavailable",
            "as_of": board_lint.format_utc_rfc3339(now),
        }
        _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
        raise WatcherError(f"Bootstrap failed: unstable or unavailable board scan ({snapshot.scan_error}).")

    route_cfg, route_corrupt = wake_routes.load_route_config(runtime_dir / ROUTES_NAME)
    if route_corrupt:
        raise WatcherError("Route config is corrupt during bootstrap.")
    registry, registry_corrupt = wake_routes.RoleRegistry.load(runtime_dir / wake_routes.REGISTRY_NAME)
    if registry_corrupt:
        raise WatcherError("Role registry is corrupt during bootstrap.")

    hk_ids, _hk_obj, hk_corrupt = board_state.load_housekeeping_receipts(runtime_dir / HOUSEKEEPING_NAME)
    if hk_corrupt:
        raise WatcherError("Housekeeping receipts file is corrupt during bootstrap.")

    wake_ledger, wake_corrupt = wake_routes.WakeLedger.load(
        runtime_dir / WAKE_LEDGER_NAME,
        now=now,
        allow_missing=not already_init,
    )
    if wake_corrupt:
        raise WatcherError("Wake ledger is corrupt or missing during bootstrap.")

    mail_ledger, mail_corrupt = wake_routes.MailLedger.load(
        runtime_dir / MAIL_LEDGER_NAME, now=now, allow_missing=not already_init,
    )
    if mail_corrupt:
        raise WatcherError("Mail ledger is corrupt during bootstrap.")

    pinned_root_id = (snapshot.root_dev, snapshot.root_ino)
    board_id = _ensure_board_id(board_dir, expected_root_id=pinned_root_id)
    manifest = board_state.build_manifest_from_snapshot(snapshot, now)
    manifest["board_id"] = board_id

    # Preliminary rebuild to identify all valid accepted UUIDs at baseline
    pre_result = board_state.rebuild(snapshot, manifest, now, housekeeping_ids=hk_ids)
    baseline_uuids = _extract_accepted_uuids(pre_result)
    result = board_state.rebuild(
        snapshot,
        manifest,
        now,
        housekeeping_ids=hk_ids,
        baseline_uuids=baseline_uuids,
    )
    health_rec = dict(result["health"])
    health_rec["as_of"] = result["as_of"]
    health_rec["snapshot_id"] = result["snapshot_id"]
    health_rec["bootstrapped"] = True

    # Write ledgers before the baseline and manifest so interrupted bootstrap can resume.
    mail_ledger.sync_auto_baseline(
        route_cfg["mail"]["enabled"], wake_routes.automatic_mail_items(result["mail_pending"]),
    )
    wake_ledger.sync_route_baselines(route_cfg, baseline_uuids, now)
    wake_ledger.save()
    if _after_wake_save_hook is not None:
        _after_wake_save_hook()
    _save_baseline(runtime_dir / BASELINE_NAME, baseline_uuids, now)
    board_state.save_manifest(runtime_dir / MANIFEST_NAME, manifest)

    board_state.publish_hub_projections(
        board_dir,
        result,
        healthy=True,
        health_record=health_rec,
        expected_root_id=pinned_root_id,
    )
    if not result["health"]["live_dispatch_suppressed"]:
        registry.sync(result["registry_events"], now)
    if not registry.path.exists():
        registry.save()
    _write_apfs_json(runtime_dir / MAIL_PENDING_NAME, result["mail_pending"])
    _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
    _write_apfs_json(runtime_dir / LAST_PUBLISHED_NAME, {"snapshot_id": result["snapshot_id"], "as_of": result["as_of"]})
    return result


def reconcile_runtime_manifest(
    board_dir: Path,
    runtime_dir: Path,
    as_of: Optional[datetime] = None,
) -> dict:
    """Explicit operator reconciliation of manifest baseline (with live dispatch disabled)."""
    ensure_non_root()
    runtime_dir = board_lint.verify_runtime_dir(runtime_dir, expected_mode=0o700, create=False)
    now = as_of or datetime.now(timezone.utc)

    snapshot = board_lint.scan_board(board_dir)
    if not snapshot.healthy:
        raise WatcherError(f"Reconciliation failed: board scan unhealthy ({snapshot.scan_error}).")

    hk_ids, _hk_obj, hk_corrupt = board_state.load_housekeeping_receipts(runtime_dir / HOUSEKEEPING_NAME)
    if hk_corrupt:
        raise WatcherError("Housekeeping receipts corrupt.")

    baseline_uuids, b_corrupt = _load_baseline(runtime_dir / BASELINE_NAME)
    if b_corrupt or baseline_uuids is None:
        baseline_uuids = set()

    pinned_root_id = (snapshot.root_dev, snapshot.root_ino)
    board_id = _ensure_board_id(board_dir, expected_root_id=pinned_root_id)
    manifest = board_state.build_manifest_from_snapshot(snapshot, now, reconcile=True)
    manifest["board_id"] = board_id
    board_state.save_manifest(runtime_dir / MANIFEST_NAME, manifest)

    result = board_state.rebuild(
        snapshot,
        manifest,
        now,
        housekeeping_ids=hk_ids,
        baseline_uuids=baseline_uuids,
    )
    health_rec = dict(result["health"])
    health_rec["as_of"] = result["as_of"]
    health_rec["snapshot_id"] = result["snapshot_id"]
    health_rec["reconciled_at"] = result["as_of"]

    board_state.publish_hub_projections(
        board_dir,
        result,
        healthy=True,
        health_record=health_rec,
        expected_root_id=pinned_root_id,
    )
    _write_apfs_json(runtime_dir / MAIL_PENDING_NAME, result["mail_pending"])
    _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
    _write_apfs_json(runtime_dir / LAST_PUBLISHED_NAME, {"snapshot_id": result["snapshot_id"], "as_of": result["as_of"]})
    return result


def _send_next_mail(
    board_dir: Path, ledger: wake_routes.MailLedger, settings: dict, digest: dict,
    clock: Callable[[], datetime], emit_log: Callable[[str], None],
) -> Optional[dict]:
    items = wake_routes.automatic_mail_items(digest)
    ledger.sync_auto_baseline(settings["enabled"], items)
    if not settings["enabled"]:
        return None
    now = clock()
    item = ledger.next_auto_item(items, now)
    if item is None:
        return None

    path = settings["notifier_path"]
    notifier = Path(path) if path else Path(board_dir).parent / "notify" / "notify_mail.py"
    submission_id = ledger.begin_submission([item["item_id"]], digest["snapshot_id"], now=now, automatic=True)
    status, code = wake_routes.run_mail_notifier(notifier, item)
    ledger.finish_submission(submission_id, status, now=clock())
    thread_id = item.get("thread_id") or "none"
    emit_log(f"ts={digest['as_of']} route=mail item={item['item_id']} status={status} code={code} thread={thread_id}")
    return {"item_id": item["item_id"], "outcome": status}


def _thread_allows_wake(thread, item) -> bool:
    if not thread:
        return False
    if item.get("terminal_notification"):
        # A human decision or task close can notify its recipient once. All
        # other messages still respect the paused or terminal conversation.
        return all(reason == "needs_user" for reason in thread["freeze_reasons"])
    return not (thread["paused"] or thread["frozen"] or thread["terminal"])


def run_tick(
    board_dir: Path,
    runtime_dir: Path,
    now_fn: Optional[Callable[[], datetime]] = None,
    agy_runner: Optional[Callable[..., Tuple[str, str]]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    allow_wake: bool = True,
    _pre_wake_hook: Optional[Callable[[], None]] = None,
    allow_mail: Optional[bool] = None,
) -> dict:
    """Scan, rebuild, publish, then dispatch at most one mail and one wake."""
    if allow_mail is None:
        allow_mail = allow_wake
    ensure_non_root()
    runtime_dir = board_lint.verify_runtime_dir(runtime_dir, expected_mode=0o700, create=True)
    clock = now_fn or (lambda: datetime.now(timezone.utc))
    now = clock()
    emit_log = log_fn or (lambda msg: LOG.info("%s", msg))

    # 1. Load APFS runtime files and detect corruption / missing bootstrap
    manifest, m_corrupt = board_state.load_manifest(runtime_dir / MANIFEST_NAME)
    baseline_uuids, b_corrupt = _load_baseline(runtime_dir / BASELINE_NAME)
    hk_ids, _hk_obj, hk_corrupt = board_state.load_housekeeping_receipts(runtime_dir / HOUSEKEEPING_NAME)
    route_cfg, r_corrupt = wake_routes.load_route_config(runtime_dir / ROUTES_NAME)
    registry, rg_corrupt = wake_routes.RoleRegistry.load(runtime_dir / wake_routes.REGISTRY_NAME)
    ledger_initialized = (
        wake_routes.is_wake_ledger_initialized(runtime_dir)
        or manifest is not None
        or m_corrupt
        or baseline_uuids is not None
        or b_corrupt
    )
    wake_ledger, wl_corrupt = wake_routes.WakeLedger.load(
        runtime_dir / WAKE_LEDGER_NAME,
        now=now,
        allow_missing=not ledger_initialized,
    )
    mail_ledger, ml_corrupt = wake_routes.MailLedger.load(
        runtime_dir / MAIL_LEDGER_NAME, now=now, allow_missing=not ledger_initialized,
    )
    last_pub, lp_corrupt = _read_last_published(runtime_dir / LAST_PUBLISHED_NAME)

    not_bootstrapped = (
        not ledger_initialized
        and manifest is None and not m_corrupt
        and baseline_uuids is None and not b_corrupt
    )
    runtime_corrupt = any((m_corrupt, b_corrupt, hk_corrupt, r_corrupt, rg_corrupt, wl_corrupt, ml_corrupt, lp_corrupt))
    if ledger_initialized and (manifest is None or baseline_uuids is None):
        runtime_corrupt = True

    if not_bootstrapped:
        health_rec = {
            "healthy": False,
            "stale": True,
            "reason": "not_bootstrapped",
            "live_dispatch_suppressed": True,
            "as_of": board_lint.format_utc_rfc3339(now),
            "last_good_snapshot_id": last_pub.get("snapshot_id") if last_pub else None,
        }
        _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
        return {"exit_code": 2, "healthy": False, "reason": "not_bootstrapped", "wake_sent": None}

    # 2. Scan board safely & verify cross-tick board identity before touching manifest or projections
    snapshot = board_lint.scan_board(board_dir)
    manifest_board_id = manifest.get("board_id") if isinstance(manifest, dict) else None
    board_identity_mismatch = (
        snapshot.healthy
        and isinstance(manifest_board_id, str)
        and not _verify_board_root_pinned(
            board_dir, snapshot.root_dev, snapshot.root_ino, expected_board_id=manifest_board_id
        )
    )
    result = board_state.rebuild(
        snapshot,
        manifest if manifest is not None else {},
        now,
        housekeeping_ids=hk_ids,
        baseline_uuids=baseline_uuids,
        runtime_corrupt=runtime_corrupt,
    )

    if not snapshot.healthy or runtime_corrupt or board_identity_mismatch:
        health_rec = dict(result["health"])
        health_rec["healthy"] = False
        health_rec["stale"] = True
        health_rec["live_dispatch_suppressed"] = True
        health_rec["as_of"] = result["as_of"]
        health_rec["last_good_snapshot_id"] = last_pub.get("snapshot_id") if last_pub else None
        if board_identity_mismatch:
            health_rec["reason"] = "board_identity_changed"
        elif runtime_corrupt:
            health_rec["reason"] = "runtime_corrupt"
        _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
        # Preserve last good projection; surface stale health on board only when identity matches
        if not board_identity_mismatch:
            with contextlib.suppress(OSError, board_state.PublishError):
                expected_id = (snapshot.root_dev, snapshot.root_ino) if snapshot.healthy else None
                board_state.publish_hub_projections(
                    board_dir, result, healthy=False, health_record=health_rec, expected_root_id=expected_id
                )
        emit_log(
            f"ts={result['as_of']} route=watch status=unhealthy code={health_rec.get('reason', 'unknown')} duration_ms=0"
        )
        return {
            "exit_code": 2,
            "healthy": False,
            "reason": health_rec.get("reason", "unhealthy"),
            "result": result,
            "wake_sent": None,
        }

    # 3. Healthy tick: persist newly observed filenames in manifest (without altering existing ones)
    updated_manifest, manifest_changed = _update_manifest_with_new_entries(snapshot, manifest, now)
    if not isinstance(updated_manifest.get("board_id"), str) and result["health"]["healthy"]:
        # Legacy manifest upgrade: bind board_id once on a clean healthy tick with zero immutability issues
        if not any(d["code"] == "IMMUTABILITY" for d in result["lint"]["diagnostics"]):
            updated_manifest = dict(updated_manifest)
            updated_manifest["board_id"] = _ensure_board_id(
                board_dir, expected_root_id=(snapshot.root_dev, snapshot.root_ino)
            )
            manifest_changed = True
    manifest_board_id = updated_manifest.get("board_id")
    if manifest_changed:
        board_state.save_manifest(runtime_dir / MANIFEST_NAME, updated_manifest)
        manifest = updated_manifest
        # Re-evaluate result with newly recorded first_observed_at
        result = board_state.rebuild(
            snapshot,
            manifest,
            now,
            housekeeping_ids=hk_ids,
            baseline_uuids=baseline_uuids,
            runtime_corrupt=False,
        )

    # 4. Sync newly opted-in route baselines so old messages never flood
    accepted_uuids = _extract_accepted_uuids(result)
    wake_ledger.sync_route_baselines(route_cfg, accepted_uuids, now)

    # 5. Replay validated registrations and collect metadata-only alerts.
    wake_ledger.refresh_codex_jobs(now)
    if not result["health"]["live_dispatch_suppressed"] and _verify_board_root_pinned(
        board_dir, snapshot.root_dev, snapshot.root_ino, expected_board_id=manifest_board_id,
    ):
        registry.sync(result["registry_events"], now)
        if not registry.path.exists():
            registry.save()
    high_incidents = [
        {
            "incident_id": board_state._make_incident_id(
                board_lint.Diagnostic(
                    severity=d["severity"],
                    code=d["code"],
                    file=d["file"],
                    message_id=d["message_id"],
                    thread_id=d["thread_id"],
                )
            ),
            "code": d["code"],
            "message_id": d["message_id"],
            "thread_id": d["thread_id"],
        }
        for d in result["lint"]["diagnostics"]
        if d["severity"] == "high"
    ]

    def publish_mail_digest(ledger, current_registry, as_of):
        digest = board_state.build_mail_digest_payload(
            snapshot_id=result["snapshot_id"], as_of=result["as_of"],
            open_pending=result["state"]["pending"], high_incidents=high_incidents,
            wake_attention_items=ledger.attention_items() + current_registry.stale_items(route_cfg, as_of),
        )
        result["mail_pending"] = digest
        _write_apfs_json(runtime_dir / MAIL_PENDING_NAME, digest)
        return digest

    mail_digest = publish_mail_digest(wake_ledger, registry, now)

    # 6. Publish _hub projections if needed
    health_rec = dict(result["health"])
    health_rec["as_of"] = result["as_of"]
    health_rec["snapshot_id"] = result["snapshot_id"]
    state_json_path = Path(board_dir) / "_hub" / "state.json"
    need_publish = (
        not last_pub
        or last_pub.get("snapshot_id") != result["snapshot_id"]
        or not state_json_path.exists()
    )
    if need_publish:
        try:
            board_state.publish_hub_projections(
                board_dir,
                result,
                healthy=True,
                health_record=health_rec,
                expected_root_id=(snapshot.root_dev, snapshot.root_ino),
            )
            _write_apfs_json(
                runtime_dir / LAST_PUBLISHED_NAME,
                {"snapshot_id": result["snapshot_id"], "as_of": result["as_of"]},
            )
        except (OSError, board_state.PublishError):
            health_rec["healthy"] = False
            health_rec["stale"] = True
            health_rec["reason"] = "publish_io_error"
            _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
            return {"exit_code": 2, "healthy": False, "reason": "publish_io_error", "result": result, "wake_sent": None}

    _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)

    mail_outcome = None
    if allow_mail and _verify_board_root_pinned(
        board_dir, snapshot.root_dev, snapshot.root_ino, expected_board_id=manifest_board_id,
    ):
        # Lint findings can suppress wakes and still trigger mail.
        latest_cfg, latest_corrupt = wake_routes.load_route_config(runtime_dir / ROUTES_NAME)
        if not latest_corrupt and latest_cfg["mail"] == route_cfg["mail"]:
            mail_outcome = _send_next_mail(board_dir, mail_ledger, route_cfg["mail"], mail_digest, clock, emit_log)

    # Select at most one eligible live wake candidate.
    wake_outcome = None
    if allow_wake and not result["health"]["live_dispatch_suppressed"]:
        candidates = []
        threads = result["state"]["threads"]
        for agent, items in sorted(result["state"]["inboxes"].items()):
            rcfg = route_cfg["routes"][agent]
            resolved, _ = registry.resolve(agent, rcfg, now, route_cfg["stale_days"])
            if not rcfg["enabled"] or resolved is None:
                continue
            for item in items:
                if item["is_broadcast"] or item["is_baseline"]:
                    continue
                thr = threads.get(item["thread_id"])
                if not _thread_allows_wake(thr, item):
                    continue
                if not wake_ledger.is_eligible(agent, item["uuid"], now):
                    continue
                if not wake_ledger.spawn_slot_available(agent, rcfg, item["thread_id"]):
                    continue
                candidates.append((item["first_observed_at"], item["sent_at"], item["number"], item["uuid"], agent))

        candidates.sort()
        if _pre_wake_hook is not None and candidates:
            _pre_wake_hook()
        for _obs_at, _sent_at, _number, msg_uuid, agent in candidates:
            recheck_now = clock()
            if not _verify_board_root_pinned(
                board_dir, snapshot.root_dev, snapshot.root_ino, expected_board_id=manifest_board_id
            ):
                health_rec = dict(result["health"])
                health_rec.update({
                    "healthy": False,
                    "stale": True,
                    "live_dispatch_suppressed": True,
                    "reason": "board_identity_changed",
                    "as_of": board_lint.format_utc_rfc3339(recheck_now),
                    "last_good_snapshot_id": last_pub.get("snapshot_id") if last_pub else None,
                })
                _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
                emit_log(
                    f"ts={health_rec['as_of']} route=watch status=unhealthy code=board_identity_changed duration_ms=0"
                )
                return {
                    "exit_code": 2,
                    "healthy": False,
                    "reason": "board_identity_changed",
                    "result": result,
                    "wake_sent": None,
                }

            re_manifest, re_mc = board_state.load_manifest(runtime_dir / MANIFEST_NAME)
            re_base, re_bc = _load_baseline(runtime_dir / BASELINE_NAME)
            re_hk, _re_hk_obj, re_hkc = board_state.load_housekeeping_receipts(runtime_dir / HOUSEKEEPING_NAME)
            re_cfg, re_rc = wake_routes.load_route_config(runtime_dir / ROUTES_NAME)
            re_registry, re_rg = wake_routes.RoleRegistry.load(runtime_dir / wake_routes.REGISTRY_NAME)
            re_wl, re_wlc = wake_routes.WakeLedger.load(runtime_dir / WAKE_LEDGER_NAME, now=recheck_now, allow_missing=False)
            _re_ml, re_mlc = wake_routes.MailLedger.load(
                runtime_dir / MAIL_LEDGER_NAME, now=recheck_now, allow_missing=False,
            )
            re_lp, re_lpc = _read_last_published(runtime_dir / LAST_PUBLISHED_NAME)
            if (
                re_manifest is None
                or re_base is None
                or any((re_mc, re_bc, re_hkc, re_rc, re_rg, re_wlc, re_mlc, re_lpc))
            ):
                health_rec = dict(result["health"])
                health_rec.update({
                    "healthy": False,
                    "stale": True,
                    "live_dispatch_suppressed": True,
                    "reason": "runtime_corrupt",
                    "as_of": board_lint.format_utc_rfc3339(recheck_now),
                    "last_good_snapshot_id": last_pub.get("snapshot_id") if last_pub else None,
                })
                _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
                emit_log(
                    f"ts={health_rec['as_of']} route=watch status=unhealthy code=runtime_corrupt duration_ms=0"
                )
                return {
                    "exit_code": 2,
                    "healthy": False,
                    "reason": "runtime_corrupt",
                    "result": result,
                    "wake_sent": None,
                }

            re_snap = board_lint.scan_board(board_dir)
            if (
                not re_snap.healthy
                or (re_snap.root_dev, re_snap.root_ino) != (snapshot.root_dev, snapshot.root_ino)
                or not _verify_board_root_pinned(
                    board_dir, snapshot.root_dev, snapshot.root_ino, expected_board_id=manifest_board_id
                )
            ):
                reason = re_snap.scan_error or "board_identity_changed"
                if re_snap.healthy:
                    reason = "board_identity_changed"
                health_rec = dict(result["health"])
                health_rec.update({
                    "healthy": False,
                    "stale": True,
                    "live_dispatch_suppressed": True,
                    "reason": reason,
                    "as_of": board_lint.format_utc_rfc3339(recheck_now),
                    "last_good_snapshot_id": last_pub.get("snapshot_id") if last_pub else None,
                })
                _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
                emit_log(
                    f"ts={health_rec['as_of']} route=watch status=unhealthy code={reason} duration_ms=0"
                )
                return {
                    "exit_code": 2,
                    "healthy": False,
                    "reason": reason,
                    "result": result,
                    "wake_sent": None,
                }

            re_res = board_state.rebuild(
                re_snap,
                re_manifest,
                recheck_now,
                housekeeping_ids=re_hk,
                baseline_uuids=re_base,
                runtime_corrupt=False,
            )
            if re_res["health"]["live_dispatch_suppressed"]:
                break
            if re_res["lint"]["counts"]["high"] > result["lint"]["counts"]["high"]:
                break

            re_rcfg = re_cfg["routes"].get(agent, {"adapter": "inbox", "enabled": False})
            if (not re_rcfg.get("enabled") or re_rcfg != route_cfg["routes"][agent]
                    or re_cfg["stale_days"] != route_cfg["stale_days"]):
                continue
            re_item = next(
                (it for it in re_res["state"]["inboxes"].get(agent, []) if it["uuid"] == msg_uuid),
                None,
            )
            if re_item is None or re_item["is_broadcast"] or re_item["is_baseline"]:
                continue
            re_thr = re_res["state"]["threads"].get(re_item["thread_id"])
            if not _thread_allows_wake(re_thr, re_item):
                continue
            if not re_wl.is_eligible(agent, msg_uuid, recheck_now):
                continue
            if re_wl.check_rate_limit(agent, recheck_now) is not None:
                continue

            # Final identity pin at dispatch boundary before calling wake()
            if not _verify_board_root_pinned(
                board_dir, snapshot.root_dev, snapshot.root_ino, expected_board_id=manifest_board_id
            ):
                dispatch_now = clock()
                health_rec = dict(result["health"])
                health_rec.update({
                    "healthy": False,
                    "stale": True,
                    "live_dispatch_suppressed": True,
                    "reason": "board_identity_changed",
                    "as_of": board_lint.format_utc_rfc3339(dispatch_now),
                    "last_good_snapshot_id": last_pub.get("snapshot_id") if last_pub else None,
                })
                _write_apfs_json(runtime_dir / HEALTH_NAME, health_rec)
                emit_log(
                    f"ts={health_rec['as_of']} route=watch status=unhealthy code=board_identity_changed duration_ms=0"
                )
                return {
                    "exit_code": 2,
                    "healthy": False,
                    "reason": "board_identity_changed",
                    "result": result,
                    "wake_sent": None,
                }

            re_registry.sync(re_res["registry_events"], recheck_now)
            if re_registry.resolve(agent, re_rcfg, recheck_now, re_cfg["stale_days"])[0] is None:
                continue
            outcome = wake_routes.wake(
                agent,
                msg_uuid,
                runtime_dir=runtime_dir,
                config=re_cfg,
                now_fn=clock,
                agy_runner=agy_runner,
                log_fn=emit_log,
                require_existing_ledger=True,
                project_dir=_project_for_board(board_dir),
                thread_id=re_item["thread_id"],
            )
            wake_outcome = {"agent": agent, "uuid": msg_uuid, "outcome": outcome}
            reloaded_wl, _ = wake_routes.WakeLedger.load(
                runtime_dir / WAKE_LEDGER_NAME, now=clock(), allow_missing=False,
            )
            reloaded_registry, _ = wake_routes.RoleRegistry.load(runtime_dir / wake_routes.REGISTRY_NAME)
            publish_mail_digest(reloaded_wl, reloaded_registry, clock())
            break

    return {
        "exit_code": 0,
        "healthy": True,
        "result": result,
        "wake_sent": wake_outcome,
        "mail_sent": mail_outcome,
    }


def _project_for_board(board_dir: Path) -> Path:
    parent = Path(board_dir).resolve().parent
    return parent.parent if parent.name == "bridge" else parent


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="BoardHub polling watcher.")
    parser.add_argument(
        "--board",
        type=Path,
        default=None,
        help="Path to board directory (env: BOARDHUB_BOARD_DIR, default: ~/boardhub/board).",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help="Private runtime directory (env: BOARDHUB_RUNTIME_DIR).",
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
    parser.add_argument("command", nargs="?", choices=("probe",), help="Check local wake route prerequisites.")
    parser.add_argument("--bootstrap", action="store_true", help="Bootstrap manifest and baseline.")
    parser.add_argument("--once", action="store_true", help="Run a single watcher tick and exit.")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Polling interval in seconds.")
    parser.add_argument("--reconcile-manifest", action="store_true", help="Reconcile manifest baseline (no wakes).")
    parser.add_argument("--housekeep", default=None, help="Record housekeeping receipt for exact pending_id.")
    parser.add_argument("--note", default="", help="Optional housekeeping note.")
    parser.add_argument("--skip-apfs-check", action="store_true", help="Skip /sbin/mount APFS check (for tests/non-macOS).")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    if args.command == "probe" and (args.bootstrap or args.once or args.reconcile_manifest or args.housekeep is not None):
        print("probe cannot be combined with watcher actions.", file=sys.stderr)
        return 2
    if args.interval < 1:
        print("Polling interval must be >= 1 second.", file=sys.stderr)
        return 2

    try:
        ensure_non_root()
        board_dir, resolved_runtime_dir, _human_role = board_lint.resolve_cli_settings(args)
        if not args.skip_apfs_check:
            verify_apfs_residence(resolved_runtime_dir)
        runtime_dir = board_lint.verify_runtime_dir(resolved_runtime_dir, expected_mode=0o700, create=args.command != "probe")
    except (WatcherError, board_lint.SecurityError, board_lint.FormatError, ValueError, OSError) as exc:
        _syslog_error(f"startup failed: {exc}")
        print(f"BoardHub startup error: {exc}", file=sys.stderr)
        return 2

    if args.command == "probe":
        config, corrupt = wake_routes.load_route_config(runtime_dir / ROUTES_NAME)
        registry, registry_corrupt = wake_routes.RoleRegistry.load(runtime_dir / wake_routes.REGISTRY_NAME)
        results = {} if corrupt or registry_corrupt else wake_routes.probe_routes(
            config, registry, _project_for_board(board_dir),
        )
        ok = not (corrupt or registry_corrupt) and all(
            route["status"] in ("ok", "inbox_only", "awaiting_checkin", "stale") for route in results.values()
        )
        report = {"ok": ok, "routes": results}
        if corrupt or registry_corrupt:
            report["error"] = "invalid_routes" if corrupt else "invalid_registry"
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if ok else 2

    try:
        log_stream = open_validated_log_stream(runtime_dir)
    except (WatcherError, board_lint.SecurityError, OSError) as exc:
        _syslog_error(f"log setup failed: {exc}")
        print(f"BoardHub log error: {exc}", file=sys.stderr)
        return 2

    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    prev_level = LOG.level
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)

    stopping = False

    def _stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        with acquire_watcher_lock(runtime_dir):
            if args.bootstrap:
                bootstrap_runtime(board_dir, runtime_dir)
                return 0
            if args.reconcile_manifest:
                reconcile_runtime_manifest(board_dir, runtime_dir)
                return 0
            if args.housekeep is not None:
                board_state.add_housekeeping_receipt(runtime_dir / HOUSEKEEPING_NAME, args.housekeep, note=args.note)
                tick_res = run_tick(board_dir, runtime_dir, allow_wake=False)
                return tick_res["exit_code"]

            if args.once:
                tick_res = run_tick(board_dir, runtime_dir)
                return tick_res["exit_code"]

            LOG.info("boardhub_watch started interval=%s", args.interval)
            while not stopping:
                run_tick(board_dir, runtime_dir)
                slept = 0
                while slept < args.interval and not stopping:
                    time.sleep(min(1, args.interval - slept))
                    slept += 1
            return 0
    except (WatcherError, board_lint.SecurityError, board_state.PublishError, ValueError, OSError) as exc:
        LOG.error("watcher stopped error=%s", type(exc).__name__)
        _syslog_error(f"watcher stopped: {type(exc).__name__}")
        return 2
    finally:
        LOG.removeHandler(handler)
        LOG.setLevel(prev_level)
        handler.close()
        log_stream.close()


if __name__ == "__main__":
    sys.exit(main())
