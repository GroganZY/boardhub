#!/usr/bin/env python3
"""BoardHub wake routes, APFS wake ledger, Gemini live adapter, and mail digest tracking.

Python 3.8+ standard library only.
"""

import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

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

if "board_state" in sys.modules:
    board_state = sys.modules["board_state"]
else:
    board_state = board_lint.load_sibling_module("board_state")


SCHEMA_VERSION = 2
RETRY_SEMANTICS_VERSION = 2
VALID_OUTCOMES = ("accepted", "not_sent", "uncertain", "disabled")
VALID_ADAPTERS = ("inbox", "gemini_agy")
DEFAULT_PYTHON_BIN = os.environ.get("BOARDHUB_PYTHON_BIN", "").strip() or "/usr/bin/python3"
DEFAULT_AGY_CLIENT = (
    Path(os.environ["BOARDHUB_AGY_CLIENT"]).expanduser()
    if os.environ.get("BOARDHUB_AGY_CLIENT", "").strip()
    else Path.home() / "boardhub" / "agy_client.py"
)


def _default_agy_client() -> str:
    env_val = os.environ.get("BOARDHUB_AGY_CLIENT", "").strip()
    if env_val:
        return str(Path(env_val).expanduser())
    return str(DEFAULT_AGY_CLIENT)


def _default_python_bin() -> str:
    return os.environ.get("BOARDHUB_PYTHON_BIN", "").strip() or DEFAULT_PYTHON_BIN

CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SAFE_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")

RECIPIENT_COOLDOWN_SECONDS = 60
GLOBAL_CAP_COUNT = 6
GLOBAL_CAP_WINDOW_SECONDS = 600
RETRY_DELAY_SECONDS = 60
OUTER_DEADLINE_SECONDS = 50.0
MAX_CLIENT_OUTPUT_BYTES = 262144

TRANSIENT_PRE_DISPATCH_CODES = frozenset({"broker_unavailable"})
NON_TRANSIENT_PRE_DISPATCH_CODES = frozenset({
    "probe_required",
    "invalid_arguments",
    "invalid_request",
    "invalid_argument",
    "unsafe_state",
    "unsafe_socket",
    "hub_unavailable",
    "bridge_unavailable",
    "agy_start_failed",
    "argv_too_large",
})

SHELL_META_CHARS = set(";&|`$><\\!\n\r\t")


class RouteConfigError(Exception):
    """Raised when APFS route config is invalid or unsafe."""


def build_wake_prompt(message_uuid: str) -> str:
    """Return fixed wake prompt carrying ONLY a validated canonical UUIDv4."""
    if not isinstance(message_uuid, str) or not board_lint.UUID_V4_RE.fullmatch(message_uuid):
        raise ValueError("message_uuid must be a canonical lowercase UUIDv4.")
    return f"New BoardHub message {message_uuid} for you."


def default_route_config() -> dict:
    """Phase 1 default: inbox-only for every recipient, live adapters disabled."""
    return {
        "version": SCHEMA_VERSION,
        "routes": {
            agent: {"adapter": "inbox", "enabled": False}
            for agent in sorted(board_lint.VALID_SENDERS)
        },
    }


def validate_route_config(raw_obj: dict) -> dict:
    """Strictly validate route config without allowing shell commands or templates."""
    if not isinstance(raw_obj, dict):
        raise RouteConfigError("Route config must be a JSON object.")
    if raw_obj.get("version") != SCHEMA_VERSION:
        raise RouteConfigError("Route config version must be 2.")
    allowed_top = {"version", "routes"}
    if set(raw_obj.keys()) - allowed_top or "routes" not in raw_obj:
        raise RouteConfigError("Unexpected or missing top-level keys in route config.")

    routes_in = raw_obj["routes"]
    if not isinstance(routes_in, dict):
        raise RouteConfigError("routes must be an object.")

    normalized_routes: Dict[str, dict] = {}
    for agent in sorted(board_lint.VALID_SENDERS):
        normalized_routes[agent] = {"adapter": "inbox", "enabled": False}

    for agent, cfg in routes_in.items():
        if agent not in board_lint.VALID_SENDERS:
            raise RouteConfigError(f"Unknown or recipient-only agent in routes: {agent}")
        if not isinstance(cfg, dict):
            raise RouteConfigError(f"Route entry for {agent} must be an object.")
        adapter = cfg.get("adapter")
        if adapter not in VALID_ADAPTERS:
            raise RouteConfigError(f"Unsupported adapter for {agent}: {adapter}")
        enabled = cfg.get("enabled")
        if type(enabled) is not bool:
            raise RouteConfigError(f"enabled for {agent} must be a boolean.")

        if adapter == "inbox":
            if set(cfg.keys()) - {"adapter", "enabled"}:
                raise RouteConfigError(f"Unexpected fields in inbox route for {agent}.")
            normalized_routes[agent] = {"adapter": "inbox", "enabled": False}
            continue

        # gemini_agy adapter (Phase 1 ONLY allows Gemini live route)
        if agent != "gemini":
            raise RouteConfigError("Phase 1 live adapter gemini_agy is allowed ONLY for gemini.")
        allowed_keys = {"adapter", "enabled", "conversation_id", "agy_client_path", "python_bin"}
        if set(cfg.keys()) - allowed_keys:
            raise RouteConfigError("Unexpected fields in gemini_agy route config.")

        conv_id = cfg.get("conversation_id")
        if conv_id is not None:
            if not isinstance(conv_id, str) or not CONVERSATION_ID_RE.fullmatch(conv_id):
                raise RouteConfigError("Invalid gemini conversation_id.")
        if enabled and not conv_id:
            raise RouteConfigError("Enabled gemini_agy route requires conversation_id.")

        client_path_str = cfg.get("agy_client_path", _default_agy_client())
        python_bin_str = cfg.get("python_bin", _default_python_bin())
        for path_label, path_val in (("agy_client_path", client_path_str), ("python_bin", python_bin_str)):
            if (
                not isinstance(path_val, str)
                or not path_val
                or not os.path.isabs(path_val)
                or any(ch in SHELL_META_CHARS for ch in path_val)
                or board_lint.has_disallowed_control_chars(path_val)
            ):
                raise RouteConfigError(f"Invalid {path_label} in route config.")

        normalized_routes[agent] = {
            "adapter": "gemini_agy",
            "enabled": enabled,
            "conversation_id": conv_id,
            "agy_client_path": client_path_str,
            "python_bin": python_bin_str,
        }

    return {"version": SCHEMA_VERSION, "routes": normalized_routes}


def load_route_config(path: Path) -> Tuple[dict, bool]:
    """Load APFS routes.json. Returns (validated_config, corrupt_or_invalid_flag)."""
    try:
        obj = board_lint.read_private_json_file(path)
    except (board_lint.SecurityError, board_lint.FormatError, OSError):
        return default_route_config(), True
    if obj is None:
        return default_route_config(), False
    try:
        return validate_route_config(obj), False
    except RouteConfigError:
        return default_route_config(), True


def _validate_executable_file(path_str: str, allow_owner_exec: bool = True) -> Path:
    path = Path(path_str)
    if not path.is_absolute():
        raise RouteConfigError("Executable path must be absolute.")
    fd = board_lint.verify_regular_file(path, expected_mode=None, allow_owner_exec=allow_owner_exec)
    os.close(fd)
    return path


def _minimal_child_env(home_dir: Optional[Path] = None) -> Dict[str, str]:
    home = str(home_dir or Path.home())
    env = {"PATH": "/usr/bin:/bin", "HOME": home}
    for key in ("USER", "LOGNAME", "TMPDIR"):
        if key in os.environ and not board_lint.has_disallowed_control_chars(os.environ[key]):
            env[key] = os.environ[key]
    if "USER" not in env or "LOGNAME" not in env:
        try:
            uname = pwd.getpwuid(os.getuid()).pw_name
            env.setdefault("USER", uname)
            env.setdefault("LOGNAME", uname)
        except KeyError:
            pass
    return env


def run_agy_client_send(
    conversation_id: str,
    prompt: str,
    agy_client_path: str = str(DEFAULT_AGY_CLIENT),
    python_bin: str = DEFAULT_PYTHON_BIN,
    timeout: float = OUTER_DEADLINE_SECONDS,
    home_dir: Optional[Path] = None,
) -> Tuple[str, str]:
    """Invoke agy_client.py send with argv, shell=False, bounded output, 50s outer deadline.

    Returns (outcome, reason_code).
    """
    if not CONVERSATION_ID_RE.fullmatch(conversation_id):
        return "not_sent", "invalid_config"

    try:
        _validate_executable_file(agy_client_path, allow_owner_exec=True)
        py_path = Path(python_bin)
        if not py_path.is_absolute() or not os.access(str(py_path), os.X_OK):
            return "not_sent", "invalid_config"
    except (board_lint.SecurityError, RouteConfigError, OSError):
        return "not_sent", "invalid_config"

    argv = [str(py_path), "-I", str(agy_client_path), "send", conversation_id, prompt]
    env = _minimal_child_env(home_dir=home_dir)

    try:
        child = subprocess.Popen(
            argv,
            shell=False,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return "not_sent", "client_start_failed"

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    deadline = time.monotonic() + timeout
    completed = False
    overflow = False
    timed_out = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, stdout_buf)
            selector.register(child.stderr, selectors.EVENT_READ, stderr_buf)
            while selector.get_map():
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    timed_out = True
                    break
                events = selector.select(remaining_time)
                if not events:
                    timed_out = True
                    break
                for key, _ in events:
                    buf = key.data
                    cap_left = MAX_CLIENT_OUTPUT_BYTES - len(buf)
                    try:
                        chunk = os.read(key.fd, min(65536, cap_left + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(chunk) > cap_left:
                        overflow = True
                        break
                    else:
                        buf.extend(chunk)
                if overflow:
                    break

        if timed_out:
            return "uncertain", "outer_timeout"
        if overflow:
            return "uncertain", "output_limit_exceeded"

        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            return "uncertain", "outer_timeout"
        returncode = child.wait(timeout=remaining_time)
        completed = True
    except subprocess.TimeoutExpired:
        return "uncertain", "outer_timeout"
    except OSError:
        return "uncertain", "client_io_failed"
    finally:
        if not completed:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(child.pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                child.kill()
        with contextlib.suppress(OSError):
            child.stdout.close()
        with contextlib.suppress(OSError):
            child.stderr.close()
        if not completed:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                child.wait(timeout=1)

    try:
        reply = board_lint.decode_json_strict(
            bytes(stdout_buf).strip(),
            max_bytes=MAX_CLIENT_OUTPUT_BYTES,
            max_depth=12,
        )
    except board_lint.FormatError:
        return "uncertain", "malformed_response"

    if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
        return "uncertain", "malformed_response"

    if returncode == 0 and reply.get("ok") is True:
        return "accepted", "accepted"

    if reply.get("ok") is False:
        err_obj = reply.get("error")
        err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
        if not isinstance(err_code, str) or not SAFE_CODE_RE.fullmatch(err_code):
            err_code = "unknown_error"
        if err_code in TRANSIENT_PRE_DISPATCH_CODES:
            return "not_sent", err_code
        if err_code in NON_TRANSIENT_PRE_DISPATCH_CODES:
            return "not_sent", err_code
        return "uncertain", err_code

    return "uncertain", "unexpected_exit_status"


# --- APFS Wake Ledger ---

WAKE_LEDGER_INIT_MARKER = ".wake_ledger.initialized"


def is_wake_ledger_initialized(runtime_dir: Path) -> bool:
    """Return True if runtime_dir has previously initialized a wake ledger or runtime baseline."""
    rdir = Path(runtime_dir)
    for name in (WAKE_LEDGER_INIT_MARKER, "manifest.json", "baseline.json", "last_published.json"):
        try:
            os.lstat(rdir / name)
            return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


class WakeLedger:
    """Durable APFS wake ledger tracking attempts, retries, rate caps, and route opt-in baselines."""

    def __init__(self, path: Path, state: dict):
        self.path = Path(path)
        self.state = state

    @classmethod
    def load(
        cls,
        path: Path,
        now: Optional[datetime] = None,
        allow_missing: bool = False,
    ) -> Tuple["WakeLedger", bool]:
        """Load wake ledger, converting any leftover 'attempting' records to 'uncertain'."""
        path = Path(path)
        try:
            obj = board_lint.read_private_json_file(path)
        except (board_lint.SecurityError, board_lint.FormatError, OSError):
            return cls(path, cls._empty_state()), True

        if obj is None:
            if allow_missing:
                return cls(path, cls._empty_state()), False
            return cls(path, cls._empty_state()), True

        if (
            obj.get("version") != SCHEMA_VERSION
            or not isinstance(obj.get("records"), dict)
            or not isinstance(obj.get("route_baselines"), dict)
        ):
            return cls(path, cls._empty_state()), True

        retry_sem_ver = obj.get("retry_semantics_version", 1)
        if isinstance(retry_sem_ver, bool) or not isinstance(retry_sem_ver, int) or retry_sem_ver < 1:
            return cls(path, cls._empty_state()), True

        now_str = board_lint.format_utc_rfc3339(now or datetime.now(timezone.utc))
        dirty = False
        needs_legacy_retry_migration = retry_sem_ver < RETRY_SEMANTICS_VERSION
        records = obj["records"]
        for key, rec in records.items():
            if not isinstance(key, str) or not isinstance(rec, dict):
                return cls(path, cls._empty_state()), True
            if rec.get("status") == "attempting":
                rec["status"] = "uncertain"
                rec["operator_action_required"] = True
                rec["last_reason_code"] = "interrupted_attempt"
                attempts = rec.get("attempts")
                if isinstance(attempts, list) and attempts and attempts[-1].get("outcome") == "attempting":
                    attempts[-1]["outcome"] = "uncertain"
                    attempts[-1]["reason_code"] = "interrupted_attempt"
                    attempts[-1]["finished_at"] = now_str
                dirty = True
            elif needs_legacy_retry_migration and rec.get("status") == "not_sent" and rec.get("transient_retryable"):
                # Pre-v0.1.2 records may reflect post-send I/O errors misclassified as broker_unavailable.
                # Mark pending retries uncertain on upgrade while preserving attempt history for review.
                rec["status"] = "uncertain"
                rec["transient_retryable"] = False
                rec["retry_after"] = None
                rec["operator_action_required"] = True
                rec["legacy_migrated_reason"] = rec.get("last_reason_code")
                rec["legacy_migrated_at"] = now_str
                rec["last_reason_code"] = "legacy_retry_uncertain"
                dirty = True

        if needs_legacy_retry_migration:
            obj["retry_semantics_version"] = RETRY_SEMANTICS_VERSION
            dirty = True

        ledger = cls(path, obj)
        if dirty:
            ledger.save()
        return ledger, False

    @staticmethod
    def _empty_state() -> dict:
        return {
            "version": SCHEMA_VERSION,
            "retry_semantics_version": RETRY_SEMANTICS_VERSION,
            "records": {},
            "route_baselines": {},
        }

    def save(self) -> None:
        payload = (json.dumps(self.state, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        board_lint.atomic_write_private_file(self.path, payload, mode=0o600)
        marker = self.path.parent / WAKE_LEDGER_INIT_MARKER
        if not marker.exists() and not marker.is_symlink():
            board_lint.atomic_write_private_file(marker, b"initialized\n", mode=0o600)

    @staticmethod
    def _key(agent: str, message_uuid: str) -> str:
        return f"{agent}:{message_uuid}"

    def sync_route_baselines(self, route_config: dict, current_accepted_uuids: Set[str], now: datetime) -> bool:
        """Record a fresh baseline when a route becomes opted-in so old messages never fire."""
        changed = False
        now_str = board_lint.format_utc_rfc3339(now)
        rb = self.state["route_baselines"]
        for agent, rcfg in sorted(route_config["routes"].items()):
            enabled = bool(rcfg.get("enabled") and rcfg.get("adapter") != "inbox")
            prev = rb.get(agent)
            if enabled:
                if not isinstance(prev, dict) or not prev.get("enabled"):
                    rb[agent] = {
                        "enabled": True,
                        "opted_in_at": now_str,
                        "baseline_uuids": sorted(current_accepted_uuids),
                    }
                    changed = True
            else:
                if isinstance(prev, dict) and prev.get("enabled"):
                    rb[agent] = {
                        "enabled": False,
                        "opted_in_at": prev.get("opted_in_at"),
                        "baseline_uuids": prev.get("baseline_uuids", []),
                    }
                    changed = True
        if changed:
            self.save()
        return changed

    def is_in_route_baseline(self, agent: str, message_uuid: str) -> bool:
        entry = self.state["route_baselines"].get(agent)
        if not isinstance(entry, dict):
            return False
        return message_uuid in entry.get("baseline_uuids", [])

    def get_record(self, agent: str, message_uuid: str) -> Optional[dict]:
        return self.state["records"].get(self._key(agent, message_uuid))

    def _all_attempt_timestamps(self) -> List[Tuple[str, datetime]]:
        out: List[Tuple[str, datetime]] = []
        for rec in self.state["records"].values():
            agent = rec.get("agent", "")
            for att in rec.get("attempts", []):
                dt = board_lint.parse_utc_rfc3339(att.get("at", ""))
                if dt is not None:
                    out.append((agent, dt))
        return out

    def check_rate_limit(self, agent: str, now: datetime) -> Optional[str]:
        """Return None if within caps, else 'recipient_rate_limited' or 'global_rate_limited'."""
        now_utc = now.astimezone(timezone.utc)
        attempts = self._all_attempt_timestamps()
        recent_global = 0
        for att_agent, att_dt in attempts:
            delta = (now_utc - att_dt).total_seconds()
            if delta < GLOBAL_CAP_WINDOW_SECONDS:
                recent_global += 1
            if att_agent == agent and delta < RECIPIENT_COOLDOWN_SECONDS:
                return "recipient_rate_limited"
        if recent_global >= GLOBAL_CAP_COUNT:
            return "global_rate_limited"
        return None

    def is_eligible(self, agent: str, message_uuid: str, now: datetime) -> bool:
        """Check whether (agent, message_uuid) can be dispatched now (ignoring rate caps)."""
        if self.is_in_route_baseline(agent, message_uuid):
            return False
        rec = self.get_record(agent, message_uuid)
        if rec is None:
            return True
        status = rec.get("status")
        if status in ("accepted", "uncertain", "attempting"):
            return False
        if status == "not_sent":
            if rec.get("attempt_count", 0) >= 2:
                return False
            if not rec.get("transient_retryable"):
                return False
            retry_after = board_lint.parse_utc_rfc3339(rec.get("retry_after", ""))
            if retry_after is None:
                return False
            return now.astimezone(timezone.utc) >= retry_after
        return False

    def begin_attempt(self, agent: str, message_uuid: str, now: datetime) -> None:
        key = self._key(agent, message_uuid)
        now_str = board_lint.format_utc_rfc3339(now)
        rec = self.state["records"].get(key)
        if rec is None:
            rec = {
                "agent": agent,
                "message_uuid": message_uuid,
                "status": "attempting",
                "attempt_count": 0,
                "attempts": [],
                "transient_retryable": False,
                "retry_after": None,
                "operator_action_required": False,
                "last_reason_code": None,
                "last_attempt_at": now_str,
            }
            self.state["records"][key] = rec
        rec["status"] = "attempting"
        rec["attempt_count"] = int(rec.get("attempt_count", 0)) + 1
        rec["last_attempt_at"] = now_str
        rec["attempts"].append({
            "at": now_str,
            "outcome": "attempting",
            "reason_code": "in_flight",
            "duration_ms": 0,
        })
        self.save()

    def finish_attempt(
        self,
        agent: str,
        message_uuid: str,
        outcome: str,
        reason_code: str,
        duration_ms: int,
        now: datetime,
    ) -> None:
        key = self._key(agent, message_uuid)
        rec = self.state["records"][key]
        now_utc = now.astimezone(timezone.utc)
        now_str = board_lint.format_utc_rfc3339(now_utc)
        rec["status"] = outcome
        rec["last_reason_code"] = reason_code

        is_transient = (
            outcome == "not_sent"
            and reason_code in TRANSIENT_PRE_DISPATCH_CODES
            and rec["attempt_count"] == 1
        )
        rec["transient_retryable"] = is_transient
        if is_transient:
            rec["retry_after"] = board_lint.format_utc_rfc3339(
                now_utc + timedelta(seconds=RETRY_DELAY_SECONDS)
            )
            rec["operator_action_required"] = False
        else:
            rec["retry_after"] = None
            rec["operator_action_required"] = outcome != "accepted"

        if rec["attempts"]:
            rec["attempts"][-1].update({
                "outcome": outcome,
                "reason_code": reason_code,
                "duration_ms": duration_ms,
                "finished_at": now_str,
            })
        self.save()

    def attention_items(self) -> List[dict]:
        """Return metadata-only operator attention items for uncertain/probe/exhausted wakes."""
        items: List[dict] = []
        for key in sorted(self.state["records"]):
            rec = self.state["records"][key]
            if not rec.get("operator_action_required"):
                continue
            status = rec.get("status")
            reason = rec.get("last_reason_code") or status
            if status == "uncertain":
                reason_code = "wake_uncertain"
            elif reason == "probe_required":
                reason_code = "probe_required"
            else:
                reason_code = "wake_failed"
            items.append({
                "item_id": f"wake:{rec['agent']}:{rec['message_uuid']}",
                "reason_code": reason_code,
                "message_id": rec["message_uuid"],
                "thread_id": None,
            })
        return items


def wake(
    agent: str,
    message_uuid: str,
    runtime_dir: Optional[Path] = None,
    config: Optional[dict] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    agy_runner: Optional[Callable[..., Tuple[str, str]]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    require_existing_ledger: Optional[bool] = None,
) -> str:
    """Dispatch a single validated wake notification according to APFS route config & ledger.

    Returns one of: 'accepted' | 'not_sent' | 'uncertain' | 'disabled'.
    """
    if agent not in board_lint.VALID_RECIPIENTS:
        raise ValueError(f"Invalid wake recipient: {agent}")
    prompt = build_wake_prompt(message_uuid)
    if agent == "all":
        return "disabled"

    clock = now_fn or (lambda: datetime.now(timezone.utc))
    now = clock()

    if config is None:
        if runtime_dir is None:
            cfg = default_route_config()
            cfg_corrupt = False
        else:
            cfg, cfg_corrupt = load_route_config(Path(runtime_dir) / "routes.json")
    else:
        try:
            cfg = validate_route_config(config)
            cfg_corrupt = False
        except RouteConfigError:
            cfg = default_route_config()
            cfg_corrupt = True

    if cfg_corrupt:
        return "not_sent"

    route = cfg["routes"].get(agent, {"adapter": "inbox", "enabled": False})
    if not route.get("enabled") or route.get("adapter") == "inbox":
        # Disabled route creates NO attempt in ledger
        return "disabled"

    ledger: Optional[WakeLedger] = None
    if runtime_dir is not None:
        rdir = Path(runtime_dir)
        ledger_path = rdir / "wake_ledger.json"
        if require_existing_ledger is None:
            allow_missing = not is_wake_ledger_initialized(rdir)
        else:
            allow_missing = not require_existing_ledger
        ledger, ledger_corrupt = WakeLedger.load(ledger_path, now=now, allow_missing=allow_missing)
        if ledger_corrupt:
            if log_fn is not None:
                log_fn(
                    f"ts={board_lint.format_utc_rfc3339(now)} route={route['adapter']} "
                    f"agent={agent} uuid={message_uuid} status=not_sent code=wake_ledger_missing_or_corrupt duration_ms=0"
                )
            return "not_sent"
        if not ledger.is_eligible(agent, message_uuid, now):
            rec = ledger.get_record(agent, message_uuid)
            if rec and rec.get("status") in ("accepted", "not_sent", "uncertain"):
                return rec["status"]
            return "disabled"
        limit_reason = ledger.check_rate_limit(agent, now)
        if limit_reason is not None:
            if log_fn is not None:
                log_fn(
                    f"ts={board_lint.format_utc_rfc3339(now)} route={route['adapter']} "
                    f"agent={agent} uuid={message_uuid} status=rate_limited code={limit_reason} duration_ms=0"
                )
            return "not_sent"

    if ledger is not None:
        ledger.begin_attempt(agent, message_uuid, now)

    t0 = time.monotonic()
    runner = agy_runner or run_agy_client_send
    try:
        outcome, reason_code = runner(
            conversation_id=route["conversation_id"],
            prompt=prompt,
            agy_client_path=route["agy_client_path"],
            python_bin=route["python_bin"],
            timeout=OUTER_DEADLINE_SECONDS,
        )
    except Exception:
        outcome, reason_code = "uncertain", "adapter_exception"
    duration_ms = max(0, int((time.monotonic() - t0) * 1000))

    if outcome not in ("accepted", "not_sent", "uncertain"):
        outcome = "uncertain"
        reason_code = "invalid_outcome"
    if not isinstance(reason_code, str) or not SAFE_CODE_RE.fullmatch(reason_code):
        reason_code = "unknown_code"

    finish_now = clock()
    if ledger is not None:
        ledger.finish_attempt(agent, message_uuid, outcome, reason_code, duration_ms, finish_now)

    if log_fn is not None:
        log_fn(
            f"ts={board_lint.format_utc_rfc3339(finish_now)} route={route['adapter']} "
            f"agent={agent} uuid={message_uuid} status={outcome} code={reason_code} duration_ms={duration_ms}"
        )

    return outcome


# --- APFS Mail Ledger for dedicated mail-only DSH session ---

class MailLedger:
    """Tracks external mail-session digest submissions in runtime state (never sends mail itself)."""

    def __init__(self, path: Path, state: dict):
        self.path = Path(path)
        self.state = state

    @classmethod
    def load(cls, path: Path, now: Optional[datetime] = None) -> Tuple["MailLedger", bool]:
        path = Path(path)
        try:
            obj = board_lint.read_private_json_file(path)
        except (board_lint.SecurityError, board_lint.FormatError, OSError):
            return cls(path, {"version": SCHEMA_VERSION, "submissions": []}), True
        if obj is None:
            return cls(path, {"version": SCHEMA_VERSION, "submissions": []}), False
        if obj.get("version") != SCHEMA_VERSION or not isinstance(obj.get("submissions"), list):
            return cls(path, {"version": SCHEMA_VERSION, "submissions": []}), True

        now_str = board_lint.format_utc_rfc3339(now or datetime.now(timezone.utc))
        dirty = False
        for rec in obj["submissions"]:
            if not isinstance(rec, dict):
                return cls(path, {"version": SCHEMA_VERSION, "submissions": []}), True
            if rec.get("status") == "attempting":
                rec["status"] = "uncertain"
                rec["finished_at"] = now_str
                dirty = True

        ledger = cls(path, obj)
        if dirty:
            ledger.save()
        return ledger, False

    def save(self) -> None:
        payload = (json.dumps(self.state, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        board_lint.atomic_write_private_file(self.path, payload, mode=0o600)

    def begin_submission(self, item_ids: List[str], snapshot_id: str, now: Optional[datetime] = None) -> str:
        if not item_ids or any(not isinstance(i, str) or not i for i in item_ids):
            raise ValueError("item_ids must be a nonempty list of IDs.")
        now_dt = now or datetime.now(timezone.utc)
        now_str = board_lint.format_utc_rfc3339(now_dt)
        sub_id = f"mail-{len(self.state['submissions']) + 1}-{snapshot_id}"
        self.state["submissions"].append({
            "submission_id": sub_id,
            "snapshot_id": snapshot_id,
            "item_ids": list(sorted(set(item_ids))),
            "status": "attempting",
            "started_at": now_str,
            "finished_at": None,
        })
        self.save()
        return sub_id

    def finish_submission(self, submission_id: str, status: str, now: Optional[datetime] = None) -> None:
        if status not in ("sent", "not_sent", "uncertain"):
            raise ValueError("Invalid mail submission status.")
        now_str = board_lint.format_utc_rfc3339(now or datetime.now(timezone.utc))
        for rec in self.state["submissions"]:
            if rec.get("submission_id") == submission_id:
                if rec.get("status") == "uncertain" and status != "uncertain":
                    # Preserve uncertain once marked uncertain on interruption/timeout
                    raise ValueError("Uncertain mail submission must remain uncertain.")
                rec["status"] = status
                rec["finished_at"] = now_str
                self.save()
                return
        raise KeyError(f"Unknown mail submission_id: {submission_id}")
