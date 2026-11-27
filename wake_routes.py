#!/usr/bin/env python3
"""BoardHub wake routes, bounded delivery adapters, and private delivery ledgers.

Python 3.8+ standard library only.
"""

import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import socket
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
ROUTE_ADAPTERS = board_lint.ROLE_ADAPTERS
REGISTRY_NAME = "registry.json"
DEFAULT_STALE_DAYS = 7
CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
CLAUDE_BIN = "/usr/local/bin/claude"
DSH_SOCKET = Path.home() / "Library/Application Support/ClaudeTunnelGuard/dsh-mcp.sock"
GEMINI_SOCKET = Path.home() / "Library/Application Support/ClaudeAgyBroker/agy.sock"
MAIL_DEFAULT_ENABLED = False
PROBE_TIMEOUT_SECONDS = 2.0
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

CONVERSATION_ID_RE = board_lint.CONVERSATION_ID_RE
SESSION_UUID_RE = board_lint.SESSION_UUID_RE
CONFIG_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
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


def build_wake_prompt(message_uuid: str, agent: str) -> str:
    """A fixed prompt containing only a canonical UUIDv4 and a known recipient."""
    if not isinstance(message_uuid, str) or not board_lint.UUID_V4_RE.fullmatch(message_uuid):
        raise ValueError("message_uuid must be a canonical lowercase UUIDv4.")
    if agent not in board_lint.VALID_RECIPIENTS:
        raise ValueError("Invalid wake recipient.")
    return f"BoardHub: new message {message_uuid} for {agent}; read bridge/board"


def default_route_config() -> dict:
    return {
        "version": SCHEMA_VERSION,
        "stale_days": DEFAULT_STALE_DAYS,
        "routes": {
            agent: {"adapter": "inbox", "enabled": False}
            for agent in sorted(board_lint.VALID_SENDERS)
        },
        "mail": {"enabled": MAIL_DEFAULT_ENABLED, "notifier_path": None},
    }


def _config_path(value, field: str) -> str:
    if (
        not isinstance(value, str)
        or not os.path.isabs(value)
        or any(ch in SHELL_META_CHARS for ch in value)
        or board_lint.has_disallowed_control_chars(value)
    ):
        raise RouteConfigError(f"{field} must be a clean absolute path.")
    return value


def _route_id(cfg: dict, field: str, pattern) -> Optional[str]:
    value = cfg.get(field)
    if value is None and not cfg.get("pin", False):
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RouteConfigError(f"Invalid or missing {field}.")
    return value


def _boolean(cfg: dict, field: str, default: bool) -> bool:
    value = cfg.get(field, default)
    if type(value) is not bool:
        raise RouteConfigError(f"{field} must be a boolean.")
    return value


def _positive_int(cfg: dict, field: str, default: int) -> int:
    value = cfg.get(field, default)
    if type(value) is not int or value < 1:
        raise RouteConfigError(f"{field} must be a positive integer.")
    return value


def _optional_token(cfg: dict, field: str) -> Optional[str]:
    value = cfg.get(field)
    if value is not None and (not isinstance(value, str) or not CONFIG_TOKEN_RE.fullmatch(value)):
        raise RouteConfigError(f"Invalid {field}.")
    return value


def _route_id_field(route: dict) -> str:
    if route["adapter"] == "gemini_agy":
        return "conversation_id"
    if route["adapter"] == "codex" and route.get("mode") == "queue":
        return "thread_id"
    return "session_id"


def validate_route_config(raw_obj: dict) -> dict:
    """Strictly validate private routes without accepting shell commands or templates."""
    if not isinstance(raw_obj, dict) or raw_obj.get("version") != SCHEMA_VERSION:
        raise RouteConfigError("Route config must be a JSON object with version 2.")
    if set(raw_obj) - {"version", "routes", "mail", "stale_days"} or not isinstance(raw_obj.get("routes"), dict):
        raise RouteConfigError("Unexpected or missing top-level keys in route config.")

    result = default_route_config()
    result["stale_days"] = _positive_int(raw_obj, "stale_days", DEFAULT_STALE_DAYS)
    for agent, cfg in raw_obj["routes"].items():
        if agent not in result["routes"]:
            raise RouteConfigError("Unknown route recipient.")
        if not isinstance(cfg, dict) or type(cfg.get("enabled")) is not bool:
            raise RouteConfigError(f"Route for {agent} requires a boolean enabled field.")
        adapter = cfg.get("adapter")
        if adapter != "inbox" and (agent not in ROUTE_ADAPTERS or adapter != ROUTE_ADAPTERS[agent]):
            raise RouteConfigError(f"Unsupported adapter for {agent}.")
        route = {"adapter": adapter, "enabled": cfg["enabled"]}
        if adapter == "inbox":
            route["enabled"] = False
        elif adapter == "gemini_agy":
            route["agy_client_path"] = _config_path(cfg.get("agy_client_path", _default_agy_client()), "agy_client_path")
            route["python_bin"] = _config_path(cfg.get("python_bin", _default_python_bin()), "python_bin")
        elif adapter == "codex":
            mode = cfg.get("mode", "exec")
            if mode not in ("exec", "resume", "queue", "spawn"):
                raise RouteConfigError("Codex mode must be resume, exec, queue or spawn.")
            route["mode"] = "exec" if mode == "resume" else mode
            route["codex_bin"] = _config_path(cfg.get("codex_bin", CODEX_BIN), "codex_bin")
            workdir = cfg.get("project_dir")
            route["project_dir"] = _config_path(workdir, "project_dir") if workdir is not None else None
            if mode != "queue":
                sandbox = cfg.get("sandbox", "workspace-write")
                if sandbox not in CODEX_SANDBOXES:
                    raise RouteConfigError("Invalid Codex sandbox.")
                route["sandbox"] = sandbox
            if mode == "spawn":
                route["max_concurrency"] = _positive_int(cfg, "max_concurrency", 1)
                for field in ("model", "effort", "service_tier"):
                    route[field] = _optional_token(cfg, field)
        elif adapter == "dsh":
            mode = cfg.get("mode", "resume")
            if mode not in ("resume", "spawn"):
                raise RouteConfigError("DSH mode must be resume or spawn.")
            route["mode"] = mode
            route["socket_path"] = _config_path(cfg.get("socket_path", str(DSH_SOCKET)), "socket_path")
            if mode == "spawn":
                cwd = cfg.get("cwd")
                route["cwd"] = _config_path(cwd, "cwd") if cwd is not None else None
                route["permissionPreset"] = _optional_token(cfg, "permissionPreset")
        elif adapter == "claude":
            route["claude_bin"] = _config_path(cfg.get("claude_bin", CLAUDE_BIN), "claude_bin")
        if route.get("mode") == "spawn":
            route["resume_same_thread"] = _boolean(cfg, "resume_same_thread", True)
        elif adapter != "inbox":
            route["pin"] = _boolean(cfg, "pin", False)
            field = _route_id_field(route)
            route[field] = _route_id(cfg, field, board_lint.SESSION_ID_PATTERNS[adapter])
        if set(cfg) - set(route):
            raise RouteConfigError(f"Unexpected fields in {agent} route.")
        result["routes"][agent] = route

    mail = raw_obj.get("mail", result["mail"])
    if not isinstance(mail, dict) or set(mail) - {"enabled", "notifier_path"} or type(mail.get("enabled")) is not bool:
        raise RouteConfigError("mail requires a boolean enabled field and accepts an optional notifier_path.")
    notifier_path = mail.get("notifier_path")
    if notifier_path is not None:
        notifier_path = _config_path(notifier_path, "notifier_path")
    if mail["enabled"] and notifier_path is None and not MAIL_DEFAULT_ENABLED:
        raise RouteConfigError("Enabled mail requires notifier_path.")
    result["mail"] = {"enabled": mail["enabled"], "notifier_path": notifier_path}
    return result


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


class RoleRegistry:
    """Private routes derived from validated, immutable board control records."""

    def __init__(self, path: Path, roles: dict):
        self.path = path
        self.roles = roles

    @classmethod
    def load(cls, path: Path) -> Tuple["RoleRegistry", bool]:
        try:
            obj = board_lint.read_private_json_file(path)
        except (board_lint.SecurityError, board_lint.FormatError, OSError):
            return cls(path, {}), True
        if obj is None:
            return cls(path, {}), False
        roles = obj.get("roles")
        if obj.get("version") != SCHEMA_VERSION or not isinstance(roles, dict):
            return cls(path, {}), True
        claimed = set()
        for role, record in roles.items():
            if role not in ROUTE_ADAPTERS or not isinstance(record, dict) or set(record) != {
                "filename", "event_id", "session_id", "sent_at", "checked_in_at", "confirmed", "last_successful_wake_at",
            }:
                return cls(path, {}), True
            filename = record.get("filename")
            match = board_lint.V2_FILENAME_RE.fullmatch(filename) if isinstance(filename, str) else None
            session = record.get("session_id")
            last_wake = record.get("last_successful_wake_at")
            adapter = ROUTE_ADAPTERS[role]
            if (
                not match or match[2] != role or match[4] != record.get("event_id")
                or not board_lint.UUID_V4_RE.fullmatch(match[4])
                or board_lint.parse_utc_rfc3339(record.get("sent_at")) is None
                or (session is None and record["checked_in_at"] is not None)
                or (session is not None and board_lint.parse_utc_rfc3339(record["checked_in_at"]) is None)
                or (last_wake is not None and board_lint.parse_utc_rfc3339(last_wake) is None)
                or type(record.get("confirmed")) is not bool
                or (session is not None and (
                    not isinstance(session, str) or not board_lint.SESSION_ID_PATTERNS[adapter].fullmatch(session)
                ))
            ):
                return cls(path, {}), True
            if session is not None and record["confirmed"]:
                if (adapter, session) in claimed:
                    return cls(path, {}), True
                claimed.add((adapter, session))
        return cls(path, roles), False

    def save(self) -> None:
        data = json.dumps({"version": SCHEMA_VERSION, "roles": self.roles}, indent=2, sort_keys=True)
        board_lint.atomic_write_private_file(self.path, (data + "\n").encode("utf-8"))

    @staticmethod
    def _order(event: dict) -> tuple:
        return board_lint.parse_utc_rfc3339(event["sent_at"]), event["filename"]

    def sync(self, events: dict, now: datetime) -> None:
        """Keep checkout tombstones and never resurrect an older or invalidated route."""
        changed = False
        for role in sorted(self.roles.keys() | events.keys()):
            old, event = self.roles.get(role), events.get(role)
            if event is not None and (old is None or self._order(event) > self._order(old)):
                checked_in_at = None
                if event["session_id"] is not None:
                    checked_in_at = board_lint.format_utc_rfc3339(min(self._order(event)[0], now))
                self.roles[role] = {
                    **event, "confirmed": True, "last_successful_wake_at": None, "checked_in_at": checked_in_at,
                }
                changed = True
            elif old is not None:
                confirmed = event is not None and event["event_id"] == old["event_id"]
                if old["confirmed"] != confirmed:
                    old["confirmed"] = confirmed
                    changed = True
        if changed:
            self.save()

    @staticmethod
    def _stale(record: dict, now: datetime, stale_days: int) -> bool:
        checked_in = board_lint.parse_utc_rfc3339(record["checked_in_at"])
        last_wake = board_lint.parse_utc_rfc3339(record["last_successful_wake_at"])
        return (now - max(checked_in, last_wake or checked_in)).total_seconds() >= stale_days * 86400

    def resolve(self, role: str, route: dict, now: datetime, stale_days: int) -> Tuple[Optional[dict], str]:
        if route["adapter"] == "inbox":
            return None, "inbox_only"
        if route.get("mode") == "spawn":
            return route, "ok"
        field = _route_id_field(route)
        record = self.roles.get(role)
        if route["pin"] or record is None:
            return (route, "ok") if route[field] else (None, "awaiting_checkin")
        session = record["session_id"]
        if session is None or not record["confirmed"]:
            return None, "awaiting_checkin"
        if self._stale(record, now, stale_days):
            return None, "stale"
        return {**route, field: session}, "ok"

    def note_wake(self, role: str, now: datetime) -> None:
        record = self.roles[role]
        previous = board_lint.parse_utc_rfc3339(record["last_successful_wake_at"])
        if previous is None or now > previous:
            record["last_successful_wake_at"] = board_lint.format_utc_rfc3339(now)
            self.save()

    def stale_items(self, config: dict, now: datetime) -> List[dict]:
        items = []
        for role, route in config["routes"].items():
            if route["enabled"] and self.resolve(role, route, now, config["stale_days"])[1] == "stale":
                event_id = self.roles[role]["event_id"]
                items.append({
                    "item_id": f"route_stale:{role}:{event_id}", "reason_code": "route_stale",
                    "message_id": event_id, "thread_id": None,
                })
        return items


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


def _run_bounded(
    argv: List[str], timeout: float, home_dir: Optional[Path] = None, cwd: Optional[Path] = None,
) -> Tuple[Optional[int], bytes, Optional[str]]:
    """Run without a shell, draining each pipe to a fixed cap and killing the group on failure."""
    env = _minimal_child_env(home_dir=home_dir)

    try:
        child = subprocess.Popen(
            argv,
            shell=False,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return None, b"", "client_start_failed"

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
            return None, b"", "outer_timeout"
        if overflow:
            return None, b"", "output_limit_exceeded"

        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            return None, b"", "outer_timeout"
        returncode = child.wait(timeout=remaining_time)
        completed = True
    except subprocess.TimeoutExpired:
        return None, b"", "outer_timeout"
    except OSError:
        return None, b"", "client_io_failed"
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

    return returncode, bytes(stdout_buf), None


def _child_failure(failure: str) -> Tuple[str, str]:
    return ("not_sent" if failure == "client_start_failed" else "uncertain"), failure


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
    returncode, stdout, failure = _run_bounded(argv, timeout, home_dir=home_dir)
    if failure:
        return _child_failure(failure)

    try:
        reply = board_lint.decode_json_strict(
            stdout.strip(),
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


def _trusted_program(path: str, executable: bool = True) -> Path:
    """Accept operator-selected user or system programs, including symlinked CLIs."""
    resolved = Path(path).resolve(strict=True)
    info = resolved.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in (0, os.getuid())
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(resolved, os.X_OK if executable else os.R_OK)
    ):
        raise OSError("Unsafe or inaccessible program.")
    # A launcher may use argv[0] to find resources relative to the original path.
    return Path(path)


def _check_broker_socket(path: Path) -> None:
    parent = path.parent.lstat()
    info = path.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
        or not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise RouteConfigError("Unsafe broker socket.")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("broker deadline")
    return remaining


def _read_dsh_reply(connection, deadline: float) -> bytes:
    reply = bytearray()
    while len(reply) <= MAX_CLIENT_OUTPUT_BYTES:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(min(65536, MAX_CLIENT_OUTPUT_BYTES + 1 - len(reply)))
        if not chunk:
            break
        reply.extend(chunk)
        if b"\n" in chunk:
            if reply.endswith(b"\n") and reply.count(b"\n") == 1:
                return bytes(reply[:-1])
            break
    raise ValueError("Invalid or oversized broker frame.")


def _dsh_call(route: dict, name: str, arguments: dict, timeout: float) -> Tuple[str, str, Optional[dict]]:
    """Send one newline-delimited JSON-RPC request through the local DSH broker."""
    deadline = time.monotonic() + timeout
    path = Path(route["socket_path"])
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    wire = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return "not_sent", "client_start_failed", None
    with connection:
        try:
            _check_broker_socket(path)
            connection.settimeout(_remaining(deadline))
            connection.connect(str(path))
        except RouteConfigError:
            return "not_sent", "unsafe_socket", None
        except OSError:
            return "not_sent", "broker_unavailable", None
        try:
            connection.settimeout(_remaining(deadline))
            connection.sendall(wire)
            envelope = board_lint.decode_json_strict(
                _read_dsh_reply(connection, deadline), max_bytes=MAX_CLIENT_OUTPUT_BYTES, max_depth=12,
            )
        except (OSError, ValueError, board_lint.FormatError):
            return "uncertain", "broker_exchange_failed", None
    response = envelope.get("response") if isinstance(envelope, dict) and envelope.get("ok") is True else None
    if (
        not isinstance(response, dict)
        or response.get("jsonrpc") != "2.0"
        or type(response.get("id")) is not int
        or response["id"] != request["id"]
        or "error" in response
    ):
        return "uncertain", "broker_rejected_or_invalid_reply", None
    result = response.get("result")
    if (
        not isinstance(result, dict)
        or result.get("isError", False) is not False
        or not (isinstance(result.get("content"), list) or isinstance(result.get("structuredContent"), dict))
    ):
        return "uncertain", "broker_rejected_or_invalid_reply", None
    return "accepted", "accepted", result


def run_dsh_continue(route: dict, prompt: str, timeout: float) -> Tuple[str, str]:
    outcome, code, _ = _dsh_call(
        route, "dsh_continue_session", {"sessionId": route["session_id"], "prompt": prompt, "wait": False}, timeout,
    )
    return outcome, code


def run_dsh_spawn(route: dict, prompt: str, workdir: Path, timeout: float) -> Tuple[str, str, Optional[str]]:
    arguments = {"cwd": str(workdir), "prompt": prompt, "wait": False}
    if route["permissionPreset"] is not None:
        arguments["permissionPreset"] = route["permissionPreset"]
    outcome, code, result = _dsh_call(route, "dsh_run_prompt", arguments, timeout)
    if outcome != "accepted":
        return outcome, code, None
    ids = []
    if isinstance(result.get("structuredContent"), dict):
        ids.append(result["structuredContent"].get("sessionId"))
    content = result.get("content")
    for item in content if isinstance(content, list) else ():
        if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
            continue
        try:
            value = board_lint.decode_json_strict(item["text"].encode("utf-8"), max_bytes=MAX_CLIENT_OUTPUT_BYTES)
        except board_lint.FormatError:
            continue
        if isinstance(value, dict):
            ids.append(value.get("sessionId"))
    ids = [value for value in ids if value is not None]
    if not ids or any(not isinstance(value, str) or not CONVERSATION_ID_RE.fullmatch(value) for value in ids):
        return "uncertain", "broker_invalid_session_id", None
    if len(set(ids)) != 1:
        return "uncertain", "broker_invalid_session_id", None
    return outcome, code, ids[0]


# A detached supervisor holds this receipt's lock until the CLI exits. The
# watcher can observe it after restart without trusting a recycled PID.
_CODEX_WORKERS: List[subprocess.Popen] = []


def _spawn_workdir(route: dict, project_dir: Optional[Path]) -> Path:
    configured = route.get("cwd") if route["adapter"] == "dsh" else route.get("project_dir")
    path = Path(configured) if configured is not None else project_dir
    if path is None or not path.is_absolute() or not path.is_dir():
        raise RouteConfigError("Spawn requires an existing project directory.")
    return path


def _spawn_target(route: dict, workdir: Path) -> str:
    endpoint = route["socket_path"] if route["adapter"] == "dsh" else route["codex_bin"]
    return hashlib.sha256(f"{route['adapter']}\0{endpoint}\0{workdir}".encode("utf-8")).hexdigest()


def _codex_spawn_argv(route: dict, workdir: Path, prompt: str, session_id: Optional[str]) -> List[str]:
    binary = str(_trusted_program(route["codex_bin"]))
    argv = [binary, "exec", "-C", str(workdir), "--sandbox", route["sandbox"], "--json"]
    if route["model"] is not None:
        argv.extend(["--model", route["model"]])
    for field, key in (("effort", "model_reasoning_effort"), ("service_tier", "service_tier")):
        if route[field] is not None:
            argv.extend(["-c", f"{key}={json.dumps(route[field])}"])
    if session_id is not None:
        argv.extend(["resume", "--skip-git-repo-check", session_id])
    else:
        argv.append("--skip-git-repo-check")
    argv.append(prompt)
    return argv


def _codex_receipt(runtime_dir: Path, uuid: str) -> Path:
    if not board_lint.UUID_V4_RE.fullmatch(uuid):
        raise ValueError("Invalid spawn receipt ID.")
    return runtime_dir / f"codex-spawn-{uuid}.jsonl"


def _start_codex_job(path: Path, argv: List[str], workdir: Path) -> Tuple[str, str]:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return "uncertain", "spawn_receipt_exists"
    except OSError:
        return "not_sent", "client_start_failed"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = subprocess.Popen(
            ["/usr/bin/python3", "-B", "-I", str(Path(__file__).resolve()), "--codex-worker", str(fd), *argv],
            cwd=workdir, env=_minimal_child_env(), start_new_session=True, pass_fds=(fd,),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _CODEX_WORKERS.append(worker)
    except OSError:
        return "not_sent", "client_start_failed"
    finally:
        os.close(fd)
    return "accepted", "spawn_started"


def _codex_worker(fd: int, argv: List[str]) -> None:
    os.umask(0o077)

    def record(**data) -> None:
        os.write(fd, (json.dumps(data, separators=(",", ":")) + "\n").encode("ascii"))
        os.fsync(fd)

    with subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(fd,),
    ) as child:
        record(pid=child.pid)
        output = child.stdout
        while True:
            line = output.readline(MAX_CLIENT_OUTPUT_BYTES + 1)
            if not line:
                break
            if not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = output.readline(MAX_CLIENT_OUTPUT_BYTES + 1)
                continue
            if b'"thread.started"' not in line:
                continue
            try:
                event = board_lint.decode_json_strict(line, max_bytes=MAX_CLIENT_OUTPUT_BYTES)
            except board_lint.FormatError:
                continue
            if not isinstance(event, dict) or event.get("type") != "thread.started":
                continue
            session = event.get("thread_id")
            if isinstance(session, str) and SESSION_UUID_RE.fullmatch(session):
                record(session_id=session)
                # Drain remaining events without storing prompts or agent output.
                for _ in iter(lambda: output.read(65536), b""):
                    pass
                break
        record(returncode=child.wait())


def _read_codex_receipt(path: Path) -> Tuple[dict, bool]:
    try:
        fd = board_lint.verify_regular_file(path)
    except board_lint.SecurityError:
        if not path.exists() and not path.is_symlink():
            return {}, False
        raise
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            running = False
        except BlockingIOError:
            running = True
        chunks = os.read(fd, 513).split(b"\n")
        data = {}
        for line in chunks[:-1]:
            data.update(board_lint.decode_json_strict(line, max_bytes=512))
        return data, running
    finally:
        os.close(fd)


def _run_cli_wake(route: dict, prompt: str, project_dir: Optional[Path], timeout: float) -> Tuple[str, str]:
    try:
        if route["adapter"] == "codex":
            if route["project_dir"] is not None:
                project_dir = Path(route["project_dir"])
            binary = str(_trusted_program(route["codex_bin"]))
            if route["mode"] == "exec":
                if project_dir is None:
                    return "not_sent", "invalid_config"
                argv = [binary, "exec", "-C", str(project_dir), "--sandbox", route["sandbox"],
                        "resume", "--skip-git-repo-check", route["session_id"], prompt]
            else:
                argv = [binary, "queue", "--thread", route["thread_id"], "--message", prompt]
        else:
            # EXPERIMENTAL: headless Claude resume. File inboxes remain available.
            binary = str(_trusted_program(route["claude_bin"]))
            argv = [binary, "-p", "--resume", route["session_id"], prompt]
    except (OSError, RuntimeError):
        return "not_sent", "invalid_config"

    returncode, _stdout, failure = _run_bounded(argv, timeout, cwd=project_dir)
    if failure:
        return _child_failure(failure)
    if returncode == 0:
        return "accepted", "accepted"
    return "uncertain", "unexpected_exit_status"


def _probe_socket(path: Path) -> None:
    _check_broker_socket(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(PROBE_TIMEOUT_SECONDS)
        connection.connect(str(path))


def probe_routes(
    config: dict, registry: Optional[RoleRegistry] = None, project_dir: Optional[Path] = None,
) -> dict:
    """Check local prerequisites. Do not spawn or write to sockets."""
    registry = registry or RoleRegistry(Path(REGISTRY_NAME), {})
    now = datetime.now(timezone.utc)
    results = {}
    for agent, route in sorted(config["routes"].items()):
        adapter = route["adapter"]
        if adapter == "inbox":
            continue
        _, status = registry.resolve(agent, route, now, config["stale_days"])
        try:
            if adapter == "gemini_agy":
                _validate_executable_file(route["agy_client_path"])
                _trusted_program(route["python_bin"])
                _probe_socket(GEMINI_SOCKET)
            elif adapter == "dsh":
                _probe_socket(Path(route["socket_path"]))
            else:
                _trusted_program(route["codex_bin"] if adapter == "codex" else route["claude_bin"])
            if route.get("mode") == "spawn":
                _spawn_workdir(route, project_dir)
        except (OSError, RuntimeError, RouteConfigError, board_lint.SecurityError):
            status = "prerequisite_unavailable"
        results[agent] = {"adapter": adapter, "enabled": route["enabled"], "status": status}
    return results


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
            spawn = rec.get("spawn")
            if spawn is not None and (
                not isinstance(spawn, dict)
                or spawn.get("adapter") not in ("codex", "dsh")
                or spawn["adapter"] != rec.get("agent")
                or not isinstance(spawn.get("thread_id"), str)
                or not board_lint.UUID_V4_RE.fullmatch(spawn["thread_id"])
                or not isinstance(spawn.get("target"), str)
                or not board_lint.SHA256_LOWER_RE.fullmatch(spawn["target"])
                or board_lint.parse_utc_rfc3339(rec.get("last_attempt_at")) is None
                or not isinstance(rec.get("message_uuid"), str)
                or not board_lint.UUID_V4_RE.fullmatch(rec["message_uuid"])
                or ("session_id" in spawn and (
                    not isinstance(spawn["session_id"], str)
                    or not board_lint.SESSION_ID_PATTERNS[spawn["adapter"]].fullmatch(spawn["session_id"])
                ))
                or ("pid" in spawn and (type(spawn["pid"]) is not int or spawn["pid"] <= 0))
            ):
                return cls(path, cls._empty_state()), True
            if rec.get("status") == "attempting" and not (spawn and spawn["adapter"] == "codex"):
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
                spawn = rcfg.get("mode") == "spawn"
                if not isinstance(prev, dict) or not prev.get("enabled") or prev.get("spawn", False) != spawn:
                    rb[agent] = {
                        "enabled": True,
                        "spawn": spawn,
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

    def begin_attempt(self, agent: str, message_uuid: str, now: datetime, spawn: Optional[dict] = None) -> None:
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
        if spawn is not None:
            rec["spawn"] = spawn
        else:
            rec.pop("spawn", None)
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

    def refresh_codex_jobs(self, now: datetime) -> None:
        """Collect only metadata from detached jobs and release finished slots."""
        _CODEX_WORKERS[:] = [worker for worker in _CODEX_WORKERS if worker.poll() is None]
        changed = False
        for rec in self.state["records"].values():
            spawn = rec.get("spawn", {})
            if rec["status"] != "attempting" or spawn.get("adapter") != "codex":
                continue
            try:
                receipt = _codex_receipt(self.path.parent, rec["message_uuid"])
                data, running = _read_codex_receipt(receipt)
            except (board_lint.SecurityError, board_lint.FormatError, OSError, TypeError, ValueError):
                # An unreadable receipt may belong to a running child. Hold its slot.
                continue
            pid, session = data.get("pid"), data.get("session_id")
            if type(pid) is int and pid > 0 and spawn.get("pid") != pid:
                spawn["pid"] = pid
                changed = True
            if isinstance(session, str) and SESSION_UUID_RE.fullmatch(session) and spawn.get("session_id") != session:
                spawn["session_id"] = session
                changed = True
            if running:
                continue
            ok = type(data.get("returncode")) is int and data["returncode"] == 0 and "session_id" in spawn
            started = board_lint.parse_utc_rfc3339(rec["last_attempt_at"])
            duration_ms = max(0, int((now - started).total_seconds() * 1000))
            self.finish_attempt(
                rec["agent"], rec["message_uuid"], "accepted" if ok else "uncertain",
                "accepted" if ok else "spawn_failed_or_interrupted", duration_ms, now,
            )
            changed = False  # finish_attempt persisted all metadata collected so far.
            with contextlib.suppress(OSError):
                receipt.unlink()
        if changed:
            self.save()

    def spawn_slot_available(self, agent: str, route: dict, thread_id: str) -> bool:
        if route.get("mode") != "spawn" or route["adapter"] != "codex":
            return True
        active = [r["spawn"] for r in self.state["records"].values()
                  if r.get("agent") == agent and r.get("status") == "attempting" and "spawn" in r]
        if route["resume_same_thread"] and any(r["thread_id"] == thread_id for r in active):
            return False
        return len(active) < route["max_concurrency"]

    def previous_session(self, agent: str, thread_id: str, target: str) -> Optional[str]:
        matches = [r for r in self.state["records"].values() if r.get("agent") == agent
                   and r.get("spawn", {}).get("thread_id") == thread_id and r.get("status") != "not_sent"]
        latest = max(matches, key=lambda r: r["last_attempt_at"], default=None)
        if latest and latest["status"] == "accepted" and latest["spawn"]["target"] == target:
            return latest["spawn"].get("session_id")
        return None

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
    project_dir: Optional[Path] = None,
    thread_id: Optional[str] = None,
) -> str:
    """Dispatch a single validated wake notification according to APFS route config & ledger.

    Returns one of: 'accepted' | 'not_sent' | 'uncertain' | 'disabled'.
    """
    if agent not in board_lint.VALID_RECIPIENTS:
        raise ValueError(f"Invalid wake recipient: {agent}")
    prompt = build_wake_prompt(message_uuid, agent)
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

    if runtime_dir is None:
        return "not_sent"

    rdir = Path(runtime_dir)
    registry, registry_corrupt = RoleRegistry.load(rdir / REGISTRY_NAME)
    if registry_corrupt:
        return "not_sent"
    route, _ = registry.resolve(agent, route, now, cfg["stale_days"])
    if route is None:
        return "disabled"
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
    ledger.refresh_codex_jobs(now)
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

    spawn = None
    argv = None
    previous = None
    if route.get("mode") == "spawn":
        thread_id = message_uuid if thread_id is None else thread_id
        if not isinstance(thread_id, str) or not board_lint.UUID_V4_RE.fullmatch(thread_id):
            raise ValueError("thread_id must be a canonical lowercase UUIDv4.")
        if not ledger.spawn_slot_available(agent, route, thread_id):
            return "not_sent"
        prompt = f"BoardHub: new message {message_uuid}; read bridge/board"
        try:
            workdir = _spawn_workdir(route, project_dir)
            target = _spawn_target(route, workdir)
            if route["resume_same_thread"]:
                previous = ledger.previous_session(agent, thread_id, target)
            if route["adapter"] == "codex":
                argv = _codex_spawn_argv(route, workdir, prompt, previous)
        except (OSError, RouteConfigError, RuntimeError):
            ledger.begin_attempt(agent, message_uuid, now)
            ledger.finish_attempt(agent, message_uuid, "not_sent", "invalid_config", 0, now)
            return "not_sent"
        spawn = {"adapter": route["adapter"], "thread_id": thread_id, "target": target}

    ledger.begin_attempt(agent, message_uuid, now, spawn=spawn)

    t0 = time.monotonic()
    try:
        if argv is not None:
            receipt = _codex_receipt(rdir, message_uuid)
            outcome, reason_code = _start_codex_job(receipt, argv, workdir)
            if outcome == "accepted":
                if log_fn is not None:
                    log_fn(f"ts={board_lint.format_utc_rfc3339(now)} route=codex agent={agent} "
                           f"uuid={message_uuid} status=accepted code=spawn_started duration_ms=0")
                return outcome  # The watcher observes the receipt on subsequent ticks.
        elif spawn is not None:
            if previous is None:
                outcome, reason_code, session_id = run_dsh_spawn(route, prompt, workdir, OUTER_DEADLINE_SECONDS)
            else:
                outcome, reason_code = run_dsh_continue({**route, "session_id": previous}, prompt, OUTER_DEADLINE_SECONDS)
                session_id = previous
            if outcome == "accepted":
                spawn["session_id"] = session_id
        elif route["adapter"] == "gemini_agy":
            runner = agy_runner or run_agy_client_send
            outcome, reason_code = runner(
                conversation_id=route["conversation_id"],
                prompt=prompt,
                agy_client_path=route["agy_client_path"],
                python_bin=route["python_bin"],
                timeout=OUTER_DEADLINE_SECONDS,
            )
        elif route["adapter"] == "dsh":
            outcome, reason_code = run_dsh_continue(route, prompt, OUTER_DEADLINE_SECONDS)
        else:
            outcome, reason_code = _run_cli_wake(route, prompt, project_dir, OUTER_DEADLINE_SECONDS)
    except Exception:
        outcome, reason_code = "uncertain", "adapter_exception"
    duration_ms = max(0, int((time.monotonic() - t0) * 1000))

    if outcome not in ("accepted", "not_sent", "uncertain"):
        outcome = "uncertain"
        reason_code = "invalid_outcome"
    if not isinstance(reason_code, str) or not SAFE_CODE_RE.fullmatch(reason_code):
        reason_code = "unknown_code"

    finish_now = clock()
    ledger.finish_attempt(agent, message_uuid, outcome, reason_code, duration_ms, finish_now)
    if outcome == "accepted" and spawn is None and not route["pin"] and agent in registry.roles:
        registry.note_wake(agent, finish_now)

    if log_fn is not None:
        log_fn(
            f"ts={board_lint.format_utc_rfc3339(finish_now)} route={route['adapter']} "
            f"agent={agent} uuid={message_uuid} status={outcome} code={reason_code} duration_ms={duration_ms}"
        )

    return outcome


# --- APFS Mail Ledger ---

class MailLedger:
    """Track automatic and legacy mail submissions in private runtime state."""

    def __init__(self, path: Path, state: dict):
        self.path = Path(path)
        self.state = state

    @classmethod
    def load(
        cls, path: Path, now: Optional[datetime] = None, allow_missing: bool = True,
    ) -> Tuple["MailLedger", bool]:
        path = Path(path)
        try:
            obj = board_lint.read_private_json_file(path)
        except (board_lint.SecurityError, board_lint.FormatError, OSError):
            return cls(path, {"version": SCHEMA_VERSION, "submissions": []}), True
        if obj is None:
            return cls(path, {"version": SCHEMA_VERSION, "submissions": []}), not allow_missing
        if obj.get("version") != SCHEMA_VERSION or not isinstance(obj.get("submissions"), list):
            return cls(path, {"version": SCHEMA_VERSION, "submissions": []}), True

        auto = obj.get("auto")
        if "auto" in obj and (
            not isinstance(auto, dict)
            or type(auto.get("enabled")) is not bool
            or not isinstance(auto.get("baseline"), list)
            or any(not isinstance(item_id, str) for item_id in auto["baseline"])
        ):
            return cls(path, {"version": SCHEMA_VERSION, "submissions": []}), True

        now_str = board_lint.format_utc_rfc3339(now or datetime.now(timezone.utc))
        dirty = False
        for rec in obj["submissions"]:
            if (
                not isinstance(rec, dict)
                or rec.get("status") not in ("attempting", "sent", "not_sent", "uncertain")
                or not isinstance(rec.get("item_ids"), list)
                or any(not isinstance(item_id, str) for item_id in rec["item_ids"])
            ):
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

    def sync_auto_baseline(self, enabled: bool, items: List[dict]) -> None:
        """On opt-in, baseline current alerts so installation and re-enable stay quiet."""
        previous = self.state.get("auto", {})
        if previous.get("enabled") is enabled:
            return
        baseline = {item["item_id"] for item in items if item["reason_code"] != "route_stale"} if enabled else set()
        self.state["auto"] = {"enabled": enabled, "baseline": sorted(baseline)}
        self.save()

    def next_auto_item(self, items: List[dict], now: datetime) -> Optional[dict]:
        attempted = set(self.state["auto"]["baseline"])
        jst = timezone(timedelta(hours=9))
        day_start = now.astimezone(jst).replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = 0
        for submission in self.state["submissions"]:
            if submission.get("automatic") or submission["status"] != "not_sent":
                attempted.update(submission["item_ids"])
            if submission.get("automatic"):
                started = board_lint.parse_utc_rfc3339(submission.get("started_at", ""))
                if started is not None and started >= day_start:
                    today_count += 1
        # The notifier independently enforces its own shared quotas, including external sends.
        if today_count >= MAIL_DAILY_ATTEMPT_CAP:
            return None
        return next((item for item in items if item["item_id"] not in attempted), None)

    def begin_submission(
        self, item_ids: List[str], snapshot_id: str, now: Optional[datetime] = None, *, automatic: bool = False,
    ) -> str:
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
            "automatic": automatic,
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


MAIL_DAILY_ATTEMPT_CAP = 20
MAIL_TITLES = {
    "needs_user": "Human review requested",
    "route_stale": "Agent route stale. Check-in needed",
    "loop_breaker_30": "Thread paused at 30 messages",
}


def automatic_mail_items(digest: dict) -> List[dict]:
    return [
        item for item in digest["items"]
        if item["kind"] == "high_incident"
        or (item["kind"] == "wake_attention" and item["reason_code"] == "route_stale")
        or (item["kind"] == "pending" and item["reason_code"] in MAIL_TITLES)
    ]


def run_mail_notifier(path: Path, item: dict, timeout: float = OUTER_DEADLINE_SECONDS) -> Tuple[str, str]:
    """Call the notifier with fixed titles and validated board metadata only."""
    try:
        notifier = _trusted_program(str(path), executable=False)
    except (OSError, RuntimeError):
        return "not_sent", "notifier_unavailable"
    title = MAIL_TITLES.get(item["reason_code"], "High-severity lint error")
    message_uuid = item.get("message_id")
    thread_uuid = item.get("thread_id")
    message_id = message_uuid if isinstance(message_uuid, str) and board_lint.UUID_V4_RE.fullmatch(message_uuid) else "none"
    thread_id = thread_uuid if isinstance(thread_uuid, str) and board_lint.UUID_V4_RE.fullmatch(thread_uuid) else "none"
    reference = message_id if message_id != "none" else thread_id
    if item["kind"] == "high_incident":
        incident_tag = item["item_id"].rsplit(":", 1)[-1]
        if re.fullmatch(r"[0-9a-f]{12}", incident_tag):
            reference = incident_tag
    subject = f"BoardHub: {title} [{reference}]"
    body = f"Title: {title}\nMessage ID: {message_id}\nThread: {thread_id}\n"
    returncode, _stdout, failure = _run_bounded(
        ["/usr/bin/python3", "-B", "-I", str(notifier), "--subject", subject, "--body", body],
        timeout,
    )
    if failure:
        return _child_failure(failure)
    if returncode == 0:
        return "sent", "sent"
    # Exit 1 can mean a quota rejection or an ambiguous SMTP failure.
    return "uncertain", "notifier_exit_failed"


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "--codex-worker":
        raise SystemExit(2)
    _codex_worker(int(sys.argv[2]), sys.argv[3:])
