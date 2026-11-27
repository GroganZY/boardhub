#!/usr/bin/env python3
"""BoardHub wake routes, bounded delivery adapters, and private delivery ledgers.

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
ROUTE_ADAPTERS = {
    "gemini": "gemini_agy",
    "codex": "codex",
    "dsh": "dsh",
    "claude_main": "claude",
    "claude_side": "claude",
}
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

CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SESSION_UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
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
    if value is None and not cfg["enabled"]:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RouteConfigError(f"Invalid or missing {field}.")
    return value


def validate_route_config(raw_obj: dict) -> dict:
    """Strictly validate private routes without accepting shell commands or templates."""
    if not isinstance(raw_obj, dict) or raw_obj.get("version") != SCHEMA_VERSION:
        raise RouteConfigError("Route config must be a JSON object with version 2.")
    if set(raw_obj) - {"version", "routes", "mail"} or not isinstance(raw_obj.get("routes"), dict):
        raise RouteConfigError("Unexpected or missing top-level keys in route config.")

    result = default_route_config()
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
            route["conversation_id"] = _route_id(cfg, "conversation_id", CONVERSATION_ID_RE)
            route["agy_client_path"] = _config_path(cfg.get("agy_client_path", _default_agy_client()), "agy_client_path")
            route["python_bin"] = _config_path(cfg.get("python_bin", _default_python_bin()), "python_bin")
        elif adapter == "codex":
            mode = cfg.get("mode", "exec")
            if mode not in ("exec", "queue"):
                raise RouteConfigError("Codex mode must be exec or queue.")
            route["mode"] = mode
            route["codex_bin"] = _config_path(cfg.get("codex_bin", CODEX_BIN), "codex_bin")
            workdir = cfg.get("project_dir")
            route["project_dir"] = _config_path(workdir, "project_dir") if workdir is not None else None
            if mode == "exec":
                route["session_id"] = _route_id(cfg, "session_id", SESSION_UUID_RE)
                sandbox = cfg.get("sandbox", "workspace-write")
                if sandbox not in CODEX_SANDBOXES:
                    raise RouteConfigError("Invalid Codex sandbox.")
                route["sandbox"] = sandbox
            else:
                route["thread_id"] = _route_id(cfg, "thread_id", SESSION_UUID_RE)
        elif adapter == "dsh":
            route["session_id"] = _route_id(cfg, "session_id", CONVERSATION_ID_RE)
            route["socket_path"] = _config_path(cfg.get("socket_path", str(DSH_SOCKET)), "socket_path")
        elif adapter == "claude":
            route["session_id"] = _route_id(cfg, "session_id", SESSION_UUID_RE)
            route["claude_bin"] = _config_path(cfg.get("claude_bin", CLAUDE_BIN), "claude_bin")
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


def run_dsh_continue(route: dict, prompt: str, timeout: float) -> Tuple[str, str]:
    """Send one newline-delimited JSON-RPC request through the local DSH broker."""
    deadline = time.monotonic() + timeout
    path = Path(route["socket_path"])
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "dsh_continue_session",
            "arguments": {"sessionId": route["session_id"], "prompt": prompt, "wait": False},
        },
    }
    wire = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return "not_sent", "client_start_failed"
    with connection:
        try:
            _check_broker_socket(path)
            connection.settimeout(_remaining(deadline))
            connection.connect(str(path))
        except RouteConfigError:
            return "not_sent", "unsafe_socket"
        except OSError:
            return "not_sent", "broker_unavailable"
        try:
            connection.settimeout(_remaining(deadline))
            connection.sendall(wire)
            envelope = board_lint.decode_json_strict(
                _read_dsh_reply(connection, deadline), max_bytes=MAX_CLIENT_OUTPUT_BYTES, max_depth=12,
            )
        except (OSError, ValueError, board_lint.FormatError):
            return "uncertain", "broker_exchange_failed"
    response = envelope.get("response") if isinstance(envelope, dict) and envelope.get("ok") is True else None
    if (
        not isinstance(response, dict)
        or response.get("jsonrpc") != "2.0"
        or type(response.get("id")) is not int
        or response["id"] != request["id"]
        or "error" in response
    ):
        return "uncertain", "broker_rejected_or_invalid_reply"
    result = response.get("result")
    if (
        not isinstance(result, dict)
        or result.get("isError", False) is not False
        or not (isinstance(result.get("content"), list) or isinstance(result.get("structuredContent"), dict))
    ):
        return "uncertain", "broker_rejected_or_invalid_reply"
    return "accepted", "accepted"


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


def probe_routes(config: dict) -> dict:
    """Check configured wake route prerequisites locally. Do not spawn or write to sockets."""
    results = {}
    for agent, route in sorted(config["routes"].items()):
        adapter = route["adapter"]
        if adapter == "inbox":
            continue
        status = "ok"
        id_field = {"gemini_agy": "conversation_id", "dsh": "session_id", "claude": "session_id",
                    "codex": "thread_id" if route.get("mode") == "queue" else "session_id"}[adapter]
        if route[id_field] is None:
            status = "missing_id"
        else:
            try:
                if adapter == "gemini_agy":
                    _validate_executable_file(route["agy_client_path"])
                    _trusted_program(route["python_bin"])
                    _probe_socket(GEMINI_SOCKET)
                elif adapter == "dsh":
                    _probe_socket(Path(route["socket_path"]))
                else:
                    _trusted_program(route["codex_bin"] if adapter == "codex" else route["claude_bin"])
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
    project_dir: Optional[Path] = None,
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

    ledger.begin_attempt(agent, message_uuid, now)

    t0 = time.monotonic()
    try:
        if route["adapter"] == "gemini_agy":
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
        self.state["auto"] = {
            "enabled": enabled,
            "baseline": sorted({item["item_id"] for item in items}) if enabled else [],
        }
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
    "loop_breaker_30": "Thread paused at 30 messages",
}


def automatic_mail_items(digest: dict) -> List[dict]:
    return [
        item for item in digest["items"]
        if item["kind"] == "high_incident"
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
