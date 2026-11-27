#!/usr/bin/env python3
"""BoardHub strict v2 parser, legacy parser, directory scanner, and lint CLI.

Python 3.8+ standard library only.
Single parser and validator shared by CLI, state rebuild, watcher, and chat v2.
"""

import argparse
import contextlib
import ctypes
import ctypes.util
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Dict, List, Optional, Tuple


# --- Protocol constants & configurable human role ---

DEFAULT_HUMAN_ROLE = "owner"
WEB_USER = "user"
ACTOR_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
AGENT_PARTICIPANTS = frozenset({
    "claude",
    "claude_main",
    "claude_side",
    "codex",
    "gpt",
    "gemini",
    "dsh",
})
HUMAN_ROLE = DEFAULT_HUMAN_ROLE
VALID_SENDERS = AGENT_PARTICIPANTS | frozenset({HUMAN_ROLE, WEB_USER})
VALID_RECIPIENTS = VALID_SENDERS | frozenset({"all"})


def validate_human_role(role: str) -> str:
    """Validate a configurable human approver role name."""
    if not isinstance(role, str) or not ACTOR_NAME_RE.fullmatch(role):
        raise ValueError("Human role must match ^[a-z0-9_]{1,32}$.")
    if role in ("all", "none") or role in AGENT_PARTICIPANTS:
        raise ValueError(f"Human role cannot collide with reserved or agent actor '{role}'.")
    return role


def configure_human_role(role: Optional[str] = None) -> str:
    """Configure the active human approver role (CLI/config/env/default 'owner')."""
    global HUMAN_ROLE, VALID_SENDERS, VALID_RECIPIENTS
    resolved = role if role is not None else (os.environ.get("BOARDHUB_HUMAN") or DEFAULT_HUMAN_ROLE)
    resolved = validate_human_role(resolved)
    HUMAN_ROLE = resolved
    VALID_SENDERS = AGENT_PARTICIPANTS | frozenset({HUMAN_ROLE, WEB_USER})
    VALID_RECIPIENTS = VALID_SENDERS | frozenset({"all"})
    if "board_state" in sys.modules:
        sys.modules["board_state"].INBOX_RECIPIENTS = tuple(sorted(VALID_SENDERS))
    return HUMAN_ROLE


if os.environ.get("BOARDHUB_HUMAN", "").strip():
    configure_human_role(os.environ.get("BOARDHUB_HUMAN", "").strip())


def default_board_dir() -> Path:
    """Return configured or default board directory (env BOARDHUB_BOARD_DIR or ~/boardhub/board)."""
    env_val = os.environ.get("BOARDHUB_BOARD_DIR", "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return Path.home() / "boardhub" / "board"


def default_runtime_dir() -> Path:
    """Return configured or platform default private runtime directory."""
    env_val = os.environ.get("BOARDHUB_RUNTIME_DIR", "").strip()
    if env_val:
        return Path(env_val).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BoardHub"
    return Path.home() / ".local" / "share" / "boardhub"


def load_app_config(config_path: Optional[Path] = None) -> dict:
    """Safely load optional BoardHub JSON config file (--config or BOARDHUB_CONFIG)."""
    raw_path = config_path if config_path is not None else os.environ.get("BOARDHUB_CONFIG", "").strip()
    if not raw_path:
        return {}
    path = Path(raw_path).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise SecurityError(f"Cannot safely open config {path.name}.") from exc
    fd_owned = True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecurityError(f"Config {path.name} is not a regular file.")
        if info.st_uid != os.getuid():
            raise SecurityError(f"Unsafe ownership on config {path.name}.")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise SecurityError(f"Config {path.name} must not be group/other writable.")
        check_no_allow_acl_fd(fd)
        fd_owned = False
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(MAX_RUNTIME_JSON_BYTES + 1)
    except BaseException:
        if fd_owned:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    if len(raw) > MAX_RUNTIME_JSON_BYTES:
        raise FormatError("FORMAT", "Config file exceeds size bound.")
    obj = decode_json_strict(raw, max_bytes=MAX_RUNTIME_JSON_BYTES, max_depth=6)
    if not isinstance(obj, dict):
        raise FormatError("FORMAT", "Config root must be a JSON object.")
    allowed_keys = {"board", "runtime_dir", "human", "agy_client_path", "python_bin"}
    extra = set(obj.keys()) - allowed_keys
    if extra:
        raise FormatError("FORMAT", f"Unknown config keys: {', '.join(sorted(extra))}")
    for key, val in obj.items():
        if not isinstance(val, str) or not val.strip() or has_disallowed_control_chars(val):
            raise FormatError("FORMAT", f"Config '{key}' must be a clean non-empty string.")
    return obj


def resolve_cli_settings(args) -> Tuple[Path, Path, str]:
    """Resolve (board_dir, runtime_dir, human_role) from CLI > env > config > defaults."""
    cfg = load_app_config(getattr(args, "config", None))
    human_arg = getattr(args, "human", None)
    human_env = os.environ.get("BOARDHUB_HUMAN", "").strip()
    human_role = configure_human_role(human_arg or human_env or cfg.get("human") or DEFAULT_HUMAN_ROLE)

    board_arg = getattr(args, "board", None)
    board_env = os.environ.get("BOARDHUB_BOARD_DIR", "").strip()
    if board_arg is not None:
        board_dir = Path(board_arg).expanduser()
    elif board_env:
        board_dir = Path(board_env).expanduser()
    elif "board" in cfg:
        board_dir = Path(cfg["board"]).expanduser()
    else:
        board_dir = Path.home() / "boardhub" / "board"

    rt_arg = getattr(args, "runtime_dir", None)
    rt_env = os.environ.get("BOARDHUB_RUNTIME_DIR", "").strip()
    if rt_arg is not None:
        runtime_dir = Path(rt_arg).expanduser()
    elif rt_env:
        runtime_dir = Path(rt_env).expanduser()
    elif "runtime_dir" in cfg:
        runtime_dir = Path(cfg["runtime_dir"]).expanduser()
    else:
        runtime_dir = default_runtime_dir()

    return board_dir, runtime_dir, human_role

ROLE_ADAPTERS = {
    "gemini": "gemini_agy",
    "codex": "codex",
    "dsh": "dsh",
    "claude": "claude",
    "claude_main": "claude",
    "claude_side": "claude",
}
REGISTRY_EVENT_TYPES = frozenset({"checkin", "checkout"})
SESSION_UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
CONVERSATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
SESSION_ID_PATTERNS = {
    "codex": SESSION_UUID_RE,
    "claude": SESSION_UUID_RE,
    "gemini_agy": CONVERSATION_ID_RE,
    "dsh": CONVERSATION_ID_RE,
}

V2_TYPES = REGISTRY_EVENT_TYPES | frozenset({
    "task",
    "state",
    "delivery",
    "review",
    "info",
    "question",
    "answer",
    "decision",
    "task_update",
})
TASK_KINDS = frozenset({"code", "coordination"})
TASK_STATES = (
    "open",
    "claimed",
    "in_progress",
    "blocked",
    "in_review",
    "done",
    "rejected",
)
TERMINAL_STATES = frozenset({"done", "rejected"})
STATE_EVENT_VALUES = frozenset({"claimed", "in_progress", "blocked"})
DECISION_VALUES = frozenset({"approve", "reject", "revise"})
REVIEW_VERDICTS = frozenset({"approve", "revise", "reject"})
PREFLIGHT_STATUSES = frozenset({"checked", "not_applicable"})

COMMON_V2_REQUIRED = (
    "v",
    "from",
    "to",
    "type",
    "request_id",
    "thread",
    "prev",
    "sent_at",
)
NEEDS_USER_FIELDS = ("needs_user", "needs_grogan", "needs_human")
KNOWN_V2_FIELDS = frozenset(COMMON_V2_REQUIRED) | frozenset(NEEDS_USER_FIELDS) | frozenset({
    "re",
    "task",
    "supersedes",
    "assignee",
    "reviewer",
    "kind",
    "state",
    "delivers",
    "review_of",
    "verdict",
    "checklist",
    "session_id",
    "decision",
    "action",
    "display_name",
    "related_thread",
})

MAX_V2_FILE_BYTES = 64 * 1024
MAX_V2_FRONTMATTER_BYTES = 24 * 1024
MAX_LEGACY_FILE_BYTES = 4 * 1024 * 1024
MAX_RUNTIME_JSON_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 6
MAX_INT_DIGITS = 256
MAX_DELIVERS_ITEMS = 32
MAX_PATH_LABEL_BYTES = 240
MAX_CHECKLIST_EVIDENCE_BYTES = 512
MAX_TASK_LABEL_BYTES = 160
DSH_CHECKLIST_COUNT = 21
LOOP_BREAKER_LIMIT = 30
STALE_HOURS = 24

UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256_LOWER_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_UTC_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|\+00:00|-00:00)$"
)
MESSAGE_SHAPED_RE = re.compile(r"^(?:\d{4}_.+|.+_to_.+)\.md$")
LEGACY_FILENAME_RE = re.compile(r"^(\d{4})_(.+)_to_(.+)\.md$")
V2_FILENAME_RE = re.compile(r"^([0-9]{4})_([a-z0-9_]+)_to_([a-z0-9_]+)--([^/]+)\.md$")
V2_CANDIDATE_FILENAME_RE = re.compile(r"^(.+)_to_(.+?)--(.+)\.md$")
LEGACY_RE_NUM_RE = re.compile(r"^\d{4}$")

ACL_TYPE_EXTENDED = 0x00000100


class SecurityError(Exception):
    """Raised when local filesystem, ownership, symlink, or ACL checks fail."""


class FormatError(Exception):
    """Raised when strict JSON or format decoding fails."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# --- Darwin ACL & APFS safety helpers ---

def _load_libc():
    lib_name = ctypes.util.find_library("c")
    if not lib_name:
        return None
    libc = ctypes.CDLL(lib_name, use_errno=True)
    if hasattr(libc, "acl_get_link_np"):
        libc.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
        libc.acl_get_link_np.restype = ctypes.c_void_p
    if hasattr(libc, "acl_get_fd_np"):
        libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.acl_get_fd_np.restype = ctypes.c_void_p
    if hasattr(libc, "acl_to_text"):
        libc.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        libc.acl_to_text.restype = ctypes.c_void_p
    if hasattr(libc, "acl_free"):
        libc.acl_free.argtypes = [ctypes.c_void_p]
        libc.acl_free.restype = ctypes.c_int
    return libc


_LIBC = _load_libc()


def _verify_acl_ptr(acl_ptr) -> None:
    if not acl_ptr:
        return
    txt_ptr = None
    try:
        length = ctypes.c_ssize_t(0)
        txt_ptr = _LIBC.acl_to_text(acl_ptr, ctypes.byref(length))
        if not txt_ptr:
            raise SecurityError("Failed to inspect extended ACL.")
        raw = ctypes.cast(txt_ptr, ctypes.c_char_p).value
        if raw is None:
            raise SecurityError("Empty extended ACL text.")
        for line in raw.decode("utf-8", errors="strict").splitlines():
            if not line or line.startswith("!#acl "):
                continue
            parts = line.split(":")
            if len(parts) < 6 or parts[4] != "deny":
                raise SecurityError("Unexpected ACL granting access.")
    except UnicodeDecodeError:
        raise SecurityError("Malformed ACL text.") from None
    finally:
        if txt_ptr:
            _LIBC.acl_free(txt_ptr)
        _LIBC.acl_free(acl_ptr)


def check_no_allow_acl_path(path: Path) -> None:
    """Fail if path has any extended ACL entry other than explicit deny."""
    if sys.platform != "darwin":
        return
    if _LIBC is None or not hasattr(_LIBC, "acl_get_link_np"):
        raise SecurityError("Darwin ACL API unavailable.")
    acl_ptr = _LIBC.acl_get_link_np(os.fsencode(path), ACL_TYPE_EXTENDED)
    _verify_acl_ptr(acl_ptr)


def check_no_allow_acl_fd(fd: int) -> None:
    """Fail if open file descriptor has any extended ACL entry other than explicit deny."""
    if sys.platform != "darwin":
        return
    if _LIBC is None or not hasattr(_LIBC, "acl_get_fd_np"):
        raise SecurityError("Darwin ACL API unavailable.")
    acl_ptr = _LIBC.acl_get_fd_np(fd, ACL_TYPE_EXTENDED)
    _verify_acl_ptr(acl_ptr)


def verify_home_ancestors(target_dir: Path) -> None:
    """When target_dir is under HOME, verify intermediate ancestor directories are safe."""
    home = Path.home()
    try:
        rel = target_dir.relative_to(home)
    except ValueError:
        return
    cur = home
    for part in rel.parts[:-1]:
        cur = cur / part
        if not cur.exists():
            continue
        info = cur.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SecurityError(f"Unsafe ancestor path {cur.name}.")
        if info.st_uid != os.getuid():
            raise SecurityError(f"Unsafe ownership on ancestor {cur.name}.")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise SecurityError(f"Writable group/other ancestor {cur.name}.")
        check_no_allow_acl_path(cur)


def verify_runtime_dir(path: Path, expected_mode: int = 0o700, create: bool = False) -> Path:
    """Verify runtime directory is non-symlink, owned by current UID, exact 0700, safe ACLs."""
    path = Path(path)
    verify_home_ancestors(path)
    if create:
        path.mkdir(mode=expected_mode, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as exc:
        raise SecurityError(f"Cannot inspect runtime directory {path.name}.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SecurityError(f"Unsafe non-directory or symlink at {path.name}.")
    if info.st_uid != os.getuid():
        raise SecurityError(f"Unsafe ownership on {path.name}.")
    if stat.S_IMODE(info.st_mode) != expected_mode:
        raise SecurityError(f"Unsafe permissions on {path.name}.")
    check_no_allow_acl_path(path)
    return path


def verify_regular_file(
    path: Path,
    expected_mode: Optional[int] = 0o600,
    allow_owner_exec: bool = False,
    allow_group_other_read: bool = False,
) -> int:
    """Open regular file with O_NOFOLLOW and verify ownership, mode, and ACLs."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise SecurityError(f"Cannot safely open {path.name}.") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecurityError(f"Unexpected file type for {path.name}.")
        if info.st_uid != os.getuid():
            raise SecurityError(f"Unsafe ownership on {path.name}.")
        mode = stat.S_IMODE(info.st_mode)
        forbidden_mask = 0o022 if allow_group_other_read else 0o077
        if mode & forbidden_mask:
            raise SecurityError(f"Unsafe group/other permissions on {path.name}.")
        if expected_mode is not None:
            if allow_owner_exec:
                if mode not in (0o600, 0o700):
                    raise SecurityError(f"Unsafe permissions on {path.name}.")
            elif mode != expected_mode:
                raise SecurityError(f"Unsafe permissions on {path.name}.")
        check_no_allow_acl_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def load_sibling_module(name: str):
    """Safely load a sibling Python module even under python3 -I isolated mode."""
    if name in sys.modules:
        return sys.modules[name]
    mod_path = Path(__file__).resolve().parent / f"{name}.py"
    fd = verify_regular_file(
        mod_path,
        expected_mode=None,
        allow_owner_exec=True,
        allow_group_other_read=True,
    )
    os.close(fd)
    spec = importlib.util.spec_from_file_location(name, str(mod_path))
    if spec is None or spec.loader is None:
        raise SecurityError(f"Unable to load sibling module {name}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def atomic_write_private_file(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Write a private APFS runtime file atomically via unique temp file, fsync, and replace."""
    parent = Path(path).parent
    tmp_name = f".{Path(path).name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
    tmp_path = parent / tmp_name
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def read_private_json_file(
    path: Path,
    max_bytes: int = MAX_RUNTIME_JSON_BYTES,
    max_depth: int = 12,
) -> Optional[dict]:
    """Safely read and strictly decode a runtime JSON file; returns None if absent."""
    try:
        fd = verify_regular_file(path, expected_mode=0o600, allow_owner_exec=False)
    except SecurityError as exc:
        if not Path(path).exists() and not Path(path).is_symlink():
            return None
        raise
    try:
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise SecurityError(f"Cannot read {Path(path).name}.") from exc
    if len(raw) > max_bytes:
        raise FormatError("RUNTIME_CORRUPT", f"{Path(path).name} exceeds size bound.")
    try:
        obj = decode_json_strict(raw, max_bytes=max_bytes, max_depth=max_depth)
    except FormatError as exc:
        raise FormatError("RUNTIME_CORRUPT", f"{Path(path).name}: {exc.detail}") from exc
    if not isinstance(obj, dict):
        raise FormatError("RUNTIME_CORRUPT", f"{Path(path).name} root must be a JSON object.")
    return obj


# --- Strict JSON & timestamp helpers ---

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON constant")


def _finite_float(text):
    value = float(text)
    if abs(value) == float("inf") or value != value:
        raise ValueError("non-finite float")
    return value


def _bounded_int(text):
    if len(text) > MAX_INT_DIGITS:
        raise ValueError("integer too large")
    return int(text)


def check_json_depth(text: str, max_depth: int = MAX_JSON_DEPTH) -> None:
    """Reject JSON nesting deeper than max_depth without risking recursion."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            if depth > max_depth:
                raise FormatError("FORMAT", "JSON nesting depth exceeded.")
        elif ch in "}]":
            if depth > 0:
                depth -= 1


def decode_json_strict(
    raw_bytes: bytes,
    max_bytes: int = MAX_V2_FRONTMATTER_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
):
    """Strictly decode UTF-8 JSON rejecting duplicate keys, NaN/Inf, and deep nesting."""
    if len(raw_bytes) > max_bytes:
        raise FormatError("FORMAT", "JSON payload exceeds size bound.")
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise FormatError("FORMAT", "JSON payload is not valid UTF-8.") from None
    check_json_depth(text, max_depth=max_depth)
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            parse_int=_bounded_int,
        )
    except (ValueError, RecursionError):
        raise FormatError("FORMAT", "Malformed JSON payload.") from None


def parse_utc_rfc3339(value: str) -> Optional[datetime]:
    """Parse a strict UTC RFC 3339 timestamp string into an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    m = RFC3339_UTC_RE.fullmatch(value)
    if not m:
        return None
    year, month, day, hour, minute, second, frac, _tz = m.groups()
    micro = 0
    if frac:
        micro = int((frac + "000000")[:6])
    try:
        return datetime(
            int(year), int(month), int(day),
            int(hour), int(minute), int(second), micro,
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def format_utc_rfc3339(dt: datetime) -> str:
    """Format an aware datetime in canonical UTC RFC 3339 Z notation."""
    utc_dt = dt.astimezone(timezone.utc)
    if utc_dt.microsecond:
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def has_disallowed_control_chars(text: str, allow_newlines: bool = False) -> bool:
    """Check for C0/C1, DEL, CR, Unicode line/paragraph separators, and bidi controls."""
    for ch in text:
        code = ord(ch)
        if (
            code == 0
            or code == 0x7F
            or (0x80 <= code <= 0x9F)
            or code in (0x2028, 0x2029)
            or (0x200E <= code <= 0x200F)
            or (0x202A <= code <= 0x202E)
            or (0x2066 <= code <= 0x2069)
        ):
            return True
        if code < 0x20:
            if not allow_newlines or ch not in ("\n", "\t"):
                return True
    return False


def escape_safe_text(value: str) -> str:
    """Render untrusted metadata safely on a single terminal/Markdown line."""
    out = []
    for ch in str(value):
        code = ord(ch)
        if code < 0x20 or code == 0x7F or (0x80 <= code <= 0x9F) or code in (0x2028, 0x2029):
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


# --- Diagnostic and record data structures ---

FIXED_DIAGNOSTIC_MESSAGES = {
    "DUPLICATE_SESSION_ID": "The adapter session already has an active check-in.",
    "IMMUTABILITY": "Manifest entry missing or modified after healthy scan.",
    "UNSAFE_ENTRY": "Message-shaped entry is a symlink or non-regular file.",
    "REQUEST_CONFLICT": "Duplicate v2 message UUID with differing bytes or route.",
    "THREAD_FORK": "Multiple valid successors compete at the same accepted predecessor.",
    "SCAN_UNAVAILABLE": "Board scan unavailable or unstable during enumeration.",
    "RUNTIME_CORRUPT": "APFS runtime state or configuration is corrupt or unsafe.",
    "FORMAT": "Malformed v2 message encoding, bounds, filename, or frontmatter schema.",
    "ROUTE_MISMATCH": "Filename route differs from frontmatter sender or recipient.",
    "REFERENCE": "Invalid thread, predecessor, cycle, or reply reference.",
    "ROLE": "Sender, recipient, or fixed task role is invalid for this event.",
    "TRANSITION": "Event type or state transition is disallowed in current task state.",
    "THREAD_CLOSED": "Event posted after thread pause or terminal task state.",
    "DELIVERY": "Malformed or missing delivery manifest.",
    "CHECKLIST": "Incomplete or malformed 21-item DSH preflight checklist.",
    "STALE_REVIEW": "Review verdict does not match active delivery UUID and digest map.",
    "DEFERRED_REF": "Predecessor or target root is missing, excluded, or unavailable.",
    "NUMBER_COLLISION": "Display number NNNN is reused across multiple messages.",
    "LEGACY_REF_AMBIGUOUS": "Legacy numeric reply matches multiple legacy messages.",
    "RETRY_ALIAS": "Byte-identical v2 message appears under an additional display number.",
}


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    file: str
    message_id: Optional[str] = None
    thread_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "file": self.file,
            "message": FIXED_DIAGNOSTIC_MESSAGES.get(self.code, self.code),
            "message_id": self.message_id,
            "thread_id": self.thread_id,
        }


@dataclass(frozen=True)
class SnapshotEntry:
    filename: str
    sha256: str
    raw_bytes: bytes
    size: int
    mtime_ns: int
    oversize_legacy: bool = False


@dataclass(frozen=True)
class UnsafeEntry:
    filename: str
    reason: str


@dataclass(frozen=True)
class BoardSnapshot:
    healthy: bool
    board_dir: str
    entries: Dict[str, SnapshotEntry] = field(default_factory=dict)
    unsafe_entries: Dict[str, UnsafeEntry] = field(default_factory=dict)
    scan_error: Optional[str] = None
    root_dev: Optional[int] = None
    root_ino: Optional[int] = None


@dataclass(frozen=True)
class ParsedV2Message:
    filename: str
    number: int
    number_str: str
    sha256: str
    size: int
    mtime_ns: int
    file_from: str
    file_to: str
    file_uuid: str
    sender: str
    recipient: str
    msg_type: str
    request_id: str
    thread: str
    prev: str
    sent_at: str
    sent_at_dt: datetime
    needs_user: bool
    re: Optional[str]
    task_label: Optional[str]
    supersedes: Optional[str]
    assignee: Optional[str]
    reviewer: Optional[str]
    kind: Optional[str]
    state: Optional[str]
    delivers: Optional[Dict[str, str]]
    review_of: Optional[dict]
    verdict: Optional[str]
    checklist: Optional[Dict[str, dict]]
    body: str
    session_id: Optional[str] = None
    decision: Optional[str] = None
    action: Optional[str] = None
    display_name: Optional[str] = None
    related_thread: Optional[str] = None


@dataclass(frozen=True)
class ParsedLegacyMessage:
    filename: str
    number: int
    number_str: str
    sha256: str
    size: int
    mtime_ns: int
    metadata: Dict[str, str]
    body: str
    decode_error: bool = False


# --- Strict JSON field validators (delivers, review_of, checklist) ---

def validate_path_label(label) -> None:
    """Validate repo-relative path label rules without opening any filesystem path."""
    if not isinstance(label, str) or not label:
        raise FormatError("DELIVERY", "Delivery path label must be a nonempty string.")
    if has_disallowed_control_chars(label, allow_newlines=False):
        raise FormatError("DELIVERY", "Delivery path label contains control characters.")
    try:
        encoded = label.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise FormatError("DELIVERY", "Delivery path label must be valid UTF-8.") from None
    if len(encoded) > MAX_PATH_LABEL_BYTES:
        raise FormatError("DELIVERY", "Delivery path label exceeds 240 UTF-8 bytes.")
    if "\\" in label or ":" in label or label.startswith("/"):
        raise FormatError("DELIVERY", "Delivery path label contains forbidden path characters.")
    segments = label.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise FormatError("DELIVERY", "Delivery path label contains empty or dot segment.")


def validate_delivers_map(obj) -> Dict[str, str]:
    """Validate a single-line JSON delivers map."""
    if not isinstance(obj, dict):
        raise FormatError("DELIVERY", "delivers must be a JSON object.")
    if len(obj) > MAX_DELIVERS_ITEMS:
        raise FormatError("DELIVERY", "delivers exceeds 32 entries.")
    out: Dict[str, str] = {}
    for key, val in obj.items():
        validate_path_label(key)
        if not isinstance(val, str) or not SHA256_LOWER_RE.fullmatch(val):
            raise FormatError("DELIVERY", "Delivery digest must be 64 lowercase hex characters.")
        out[key] = val
    return out


def validate_review_of(obj) -> dict:
    """Validate review_of JSON object {id, delivers}."""
    if not isinstance(obj, dict) or set(obj.keys()) != {"id", "delivers"}:
        raise FormatError("FORMAT", "review_of must be an object with exact keys id and delivers.")
    delivery_id = obj["id"]
    if not isinstance(delivery_id, str) or not UUID_V4_RE.fullmatch(delivery_id):
        raise FormatError("FORMAT", "review_of.id must be a canonical lowercase UUIDv4.")
    delivers = validate_delivers_map(obj["delivers"])
    return {"id": delivery_id, "delivers": delivers}


def validate_checklist(obj) -> Dict[str, dict]:
    """Validate DSH 21-item preflight checklist JSON object."""
    if not isinstance(obj, dict):
        raise FormatError("CHECKLIST", "checklist must be a JSON object.")
    expected_keys = {str(i) for i in range(1, DSH_CHECKLIST_COUNT + 1)}
    if set(obj.keys()) != expected_keys:
        raise FormatError("CHECKLIST", "checklist must contain exact string keys '1'..'21'.")
    out: Dict[str, dict] = {}
    for key in sorted(expected_keys, key=int):
        entry = obj[key]
        if not isinstance(entry, dict) or set(entry.keys()) != {"status", "evidence"}:
            raise FormatError("CHECKLIST", f"checklist['{key}'] must have keys status and evidence.")
        status = entry["status"]
        evidence = entry["evidence"]
        if not isinstance(status, str) or status not in PREFLIGHT_STATUSES:
            raise FormatError("CHECKLIST", f"checklist['{key}'].status invalid.")
        if not isinstance(evidence, str):
            raise FormatError("CHECKLIST", f"checklist['{key}'].evidence must be a string.")
        if has_disallowed_control_chars(evidence, allow_newlines=True):
            raise FormatError("CHECKLIST", f"checklist['{key}'].evidence contains disallowed controls.")
        try:
            encoded = evidence.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise FormatError("CHECKLIST", f"checklist['{key}'].evidence invalid UTF-8.") from None
        if not evidence.strip() or len(encoded) > MAX_CHECKLIST_EVIDENCE_BYTES:
            raise FormatError("CHECKLIST", f"checklist['{key}'].evidence blank or exceeds 512 bytes.")
        out[key] = {"status": status, "evidence": evidence}
    return out


# --- Tolerant legacy parser (compatible with board_chat.py) ---

def _legacy_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def is_yes(value) -> bool:
    return str(value).strip().lower() in ("yes", "true", "on", "1")


def parse_legacy_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        return {}, text
    metadata: Dict[str, str] = {}
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match:
            metadata[match[1].lower()] = _legacy_scalar(match[2])
    return metadata, "\n".join(lines[end + 1:]).lstrip("\n")


def parse_legacy_message(entry: SnapshotEntry) -> ParsedLegacyMessage:
    m = LEGACY_FILENAME_RE.fullmatch(entry.filename)
    num_str = m[1] if m else "0000"
    num = int(num_str)
    file_from = m[2] if m else ""
    file_to = m[3] if m else ""
    try:
        text = entry.raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParsedLegacyMessage(
            filename=entry.filename,
            number=num,
            number_str=num_str,
            sha256=entry.sha256,
            size=entry.size,
            mtime_ns=entry.mtime_ns,
            metadata={"from": file_from, "to": file_to},
            body="",
            decode_error=True,
        )
    metadata, body = parse_legacy_frontmatter(text)
    metadata["from"] = metadata.get("from") or file_from
    metadata["to"] = metadata.get("to") or file_to
    return ParsedLegacyMessage(
        filename=entry.filename,
        number=num,
        number_str=num_str,
        sha256=entry.sha256,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        metadata=metadata,
        body=body,
        decode_error=False,
    )


# --- Strict v2 parser ---

def is_v2_candidate_filename(filename: str) -> bool:
    return bool(V2_CANDIDATE_FILENAME_RE.fullmatch(filename))


def _v2_fail(
    code: str,
    filename: str,
    message_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Diagnostic:
    return Diagnostic(
        severity="error",
        code=code,
        file=filename,
        message_id=message_id if (message_id and UUID_V4_RE.fullmatch(message_id)) else None,
        thread_id=thread_id if (thread_id and UUID_V4_RE.fullmatch(thread_id)) else None,
    )


def parse_v2_message(entry: SnapshotEntry) -> Tuple[Optional[ParsedV2Message], List[Diagnostic]]:
    """Parse and strictly validate a single v2 message file without thread context."""
    filename = entry.filename
    m_file = V2_FILENAME_RE.fullmatch(filename)
    file_uuid_hint: Optional[str] = None
    if m_file:
        num_str, file_from, file_to, file_uuid = m_file.groups()
        if UUID_V4_RE.fullmatch(file_uuid):
            file_uuid_hint = file_uuid
        if file_from not in VALID_SENDERS or file_to not in VALID_RECIPIENTS or not UUID_V4_RE.fullmatch(file_uuid):
            return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]
    else:
        return None, [_v2_fail("FORMAT", filename)]

    if entry.size > MAX_V2_FILE_BYTES or len(entry.raw_bytes) > MAX_V2_FILE_BYTES:
        return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]

    try:
        text = entry.raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]

    if text.startswith("\ufeff"):
        return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]

    # Split strictly on LF so stray CR in metadata is caught cleanly.
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]

    end = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end = i
            break
    if end is None:
        return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]

    fm_bytes = len("\n".join(lines[:end + 1]).encode("utf-8"))
    if fm_bytes > MAX_V2_FRONTMATTER_BYTES:
        return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]

    meta: Dict[str, str] = {}
    for line in lines[1:end]:
        if has_disallowed_control_chars(line, allow_newlines=False):
            return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$", line)
        if not km:
            return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]
        key = km[1]
        raw_val = km[2]
        if raw_val and raw_val[0] not in (" ", "\t"):
            # Allow `key:value` only if non-empty; wait: standard YAML requires space or empty;
            # reject jammed tokens if needed? Actually `key: value` has leading space; strip single/multi spaces.
            pass
        val = raw_val.strip(" ")
        if key not in KNOWN_V2_FIELDS:
            return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]
        if key in meta:
            return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]
        if "\t" in val:
            return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]
        meta[key] = val

    body = "\n".join(lines[end + 1:])
    if not body.strip():
        return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]
    # Normalize leading blank lines after closing --- for display, keep exact emptiness check above.
    body_display = body.lstrip("\n")

    # Validate required common v2 fields
    for req in COMMON_V2_REQUIRED:
        if req not in meta or meta[req] == "":
            return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]

    if meta["v"] != "2":
        return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]

    req_id = meta["request_id"]
    thread = meta["thread"]
    prev = meta["prev"]
    if not UUID_V4_RE.fullmatch(req_id) or req_id != file_uuid:
        return None, [_v2_fail("FORMAT", filename, file_uuid_hint)]
    if not UUID_V4_RE.fullmatch(thread):
        return None, [_v2_fail("FORMAT", filename, req_id)]
    if prev != "none" and not UUID_V4_RE.fullmatch(prev):
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]

    sender = meta["from"]
    recipient = meta["to"]
    if sender not in VALID_SENDERS or recipient not in VALID_RECIPIENTS:
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]
    if sender != file_from or recipient != file_to:
        return None, [_v2_fail("ROUTE_MISMATCH", filename, req_id, thread)]

    msg_type = meta["type"]
    if msg_type not in V2_TYPES:
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]

    sent_at_str = meta["sent_at"]
    sent_at_dt = parse_utc_rfc3339(sent_at_str)
    if sent_at_dt is None:
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]

    present_needs = [key for key in NEEDS_USER_FIELDS if key in meta]
    if len(present_needs) != 1:
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]
    nu_raw = meta[present_needs[0]]
    if nu_raw not in ("yes", "no"):
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]
    needs_user = nu_raw == "yes"

    # Root vs non-root consistency
    is_root = prev == "none"
    if is_root and thread != req_id:
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]
    if not is_root and thread == req_id:
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]
    if not is_root and prev == req_id:
        return None, [_v2_fail("REFERENCE", filename, req_id, thread)]

    re_id: Optional[str] = None
    if "re" in meta:
        re_id = meta["re"]
        if not UUID_V4_RE.fullmatch(re_id) or re_id == req_id or is_root:
            return None, [_v2_fail("REFERENCE", filename, req_id, thread)]

    task_label: Optional[str] = None
    if "task" in meta:
        if not is_root:
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]
        task_label = meta["task"]
        if not task_label or len(task_label.encode("utf-8")) > MAX_TASK_LABEL_BYTES:
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]

    supersedes: Optional[str] = None
    if "supersedes" in meta:
        if msg_type != "task" or not is_root:
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]
        supersedes = meta["supersedes"]
        if not UUID_V4_RE.fullmatch(supersedes) or supersedes == req_id:
            return None, [_v2_fail("REFERENCE", filename, req_id, thread)]

    # Allowed optional/required fields by type
    allowed_type_fields = {
        "task": {"assignee", "reviewer", "kind", "task", "supersedes"},
        "state": {"state", "re"},
        "delivery": {"delivers", "checklist", "re"},
        "review": {"review_of", "verdict", "re"},
        "info": {"task", "re", "related_thread"},
        "question": {"task", "re"},
        "answer": {"re"},
        "decision": {"decision"},
        "task_update": {"action", "assignee"},
        "checkin": {"session_id"},
        "checkout": set(),
    }[msg_type]
    if sender == WEB_USER:
        allowed_type_fields.add("display_name")
    extra_keys = set(meta.keys()) - set(COMMON_V2_REQUIRED) - set(NEEDS_USER_FIELDS) - allowed_type_fields
    if extra_keys:
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]

    assignee: Optional[str] = None
    reviewer: Optional[str] = None
    kind: Optional[str] = None
    state: Optional[str] = None
    delivers: Optional[Dict[str, str]] = None
    review_of: Optional[dict] = None
    verdict: Optional[str] = None
    checklist: Optional[Dict[str, dict]] = None

    decision = meta.get("decision")
    action = meta.get("action")
    display_name = meta.get("display_name")
    related_thread = meta.get("related_thread")
    if display_name is not None and (not display_name or len(display_name.encode("utf-8")) > 120):
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]
    if related_thread is not None and (
        sender != WEB_USER or not is_root or related_thread == thread
        or not UUID_V4_RE.fullmatch(related_thread)
    ):
        return None, [_v2_fail("REFERENCE", filename, req_id, thread)]
    if msg_type in ("decision", "task_update"):
        if sender != WEB_USER or recipient not in AGENT_PARTICIPANTS | {"all"} or needs_user:
            return None, [_v2_fail("ROLE", filename, req_id, thread)]
    if decision is not None and (is_root or decision not in DECISION_VALUES):
        return None, [_v2_fail("FORMAT", filename, req_id, thread)]
    if msg_type == "task_update":
        assignee = meta.get("assignee")
        if is_root or action not in ("reassign", "close") or recipient not in AGENT_PARTICIPANTS:
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]
        if (action == "reassign" and assignee != recipient) or (action == "close" and assignee is not None):
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]

    session_id = meta.get("session_id")
    if msg_type in REGISTRY_EVENT_TYPES:
        if sender not in ROLE_ADAPTERS:
            return None, [_v2_fail("ROLE", filename, req_id, thread)]
        if not is_root or needs_user:
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]
        if msg_type == "checkin" and (
            session_id is None or not SESSION_ID_PATTERNS[ROLE_ADAPTERS[sender]].fullmatch(session_id)
        ):
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]

    if msg_type == "task":
        if not is_root:
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]
        if sender not in ("claude_main", "claude", WEB_USER):
            return None, [_v2_fail("ROLE", filename, req_id, thread)]
        for k in ("assignee", "reviewer", "kind"):
            if k not in meta or not meta[k]:
                return None, [_v2_fail("FORMAT", filename, req_id, thread)]
        assignee = meta["assignee"]
        reviewer_raw = meta["reviewer"]
        kind = meta["kind"]
        if assignee not in AGENT_PARTICIPANTS:
            return None, [_v2_fail("ROLE", filename, req_id, thread)]
        if recipient != assignee:
            return None, [_v2_fail("ROLE", filename, req_id, thread)]
        if kind not in TASK_KINDS:
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]
        if reviewer_raw == "none":
            reviewer = None
        elif reviewer_raw in AGENT_PARTICIPANTS:
            reviewer = reviewer_raw
        else:
            return None, [_v2_fail("ROLE", filename, req_id, thread)]
        if reviewer == assignee:
            return None, [_v2_fail("ROLE", filename, req_id, thread)]
        if assignee == "dsh" and kind == "code" and reviewer != "codex":
            return None, [_v2_fail("ROLE", filename, req_id, thread)]

    elif msg_type == "state":
        if is_root:
            return None, [_v2_fail("TRANSITION", filename, req_id, thread)]
        state = meta.get("state", "")
        if state not in STATE_EVENT_VALUES:
            return None, [_v2_fail("TRANSITION", filename, req_id, thread)]

    elif msg_type == "delivery":
        if is_root:
            return None, [_v2_fail("TRANSITION", filename, req_id, thread)]
        if "delivers" in meta:
            try:
                raw_delivers = decode_json_strict(
                    meta["delivers"].encode("utf-8"),
                    max_bytes=MAX_V2_FRONTMATTER_BYTES,
                    max_depth=MAX_JSON_DEPTH,
                )
                delivers = validate_delivers_map(raw_delivers)
            except FormatError as exc:
                return None, [_v2_fail(exc.code, filename, req_id, thread)]
        if "checklist" in meta:
            try:
                raw_checklist = decode_json_strict(
                    meta["checklist"].encode("utf-8"),
                    max_bytes=MAX_V2_FRONTMATTER_BYTES,
                    max_depth=MAX_JSON_DEPTH,
                )
                checklist = validate_checklist(raw_checklist)
            except FormatError as exc:
                return None, [_v2_fail(exc.code, filename, req_id, thread)]

    elif msg_type == "review":
        if is_root:
            return None, [_v2_fail("TRANSITION", filename, req_id, thread)]
        if "review_of" not in meta or "verdict" not in meta:
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]
        verdict = meta["verdict"]
        if verdict not in REVIEW_VERDICTS:
            return None, [_v2_fail("FORMAT", filename, req_id, thread)]
        try:
            raw_review_of = decode_json_strict(
                meta["review_of"].encode("utf-8"),
                max_bytes=MAX_V2_FRONTMATTER_BYTES,
                max_depth=MAX_JSON_DEPTH,
            )
            review_of = validate_review_of(raw_review_of)
        except FormatError as exc:
            return None, [_v2_fail(exc.code, filename, req_id, thread)]

    elif msg_type == "answer":
        if is_root or re_id is None:
            return None, [_v2_fail("REFERENCE", filename, req_id, thread)]

    return ParsedV2Message(
        filename=filename,
        number=int(num_str),
        number_str=num_str,
        sha256=entry.sha256,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        file_from=file_from,
        file_to=file_to,
        file_uuid=file_uuid,
        sender=sender,
        recipient=recipient,
        msg_type=msg_type,
        request_id=req_id,
        thread=thread,
        prev=prev,
        sent_at=sent_at_str,
        sent_at_dt=sent_at_dt,
        needs_user=needs_user,
        re=re_id,
        task_label=task_label,
        supersedes=supersedes,
        assignee=assignee,
        reviewer=reviewer,
        kind=kind,
        state=state,
        delivers=delivers,
        review_of=review_of,
        verdict=verdict,
        checklist=checklist,
        body=body_display,
        session_id=session_id,
        decision=decision,
        action=action,
        display_name=display_name,
        related_thread=related_thread,
    ), []


# --- Board scanning (two-pass stable inventory with O_NOFOLLOW) ---

def is_staging_entry(name: str) -> bool:
    """Return True for hidden, AppleDouble, or staging/in-progress filenames ignored by readers."""
    if name.startswith("._") or name.startswith("."):
        return True
    lower = name.lower()
    if lower.startswith(("tmp_", "partial_")):
        return True
    if lower.endswith((".tmp", ".part", ".partial", ".tmp.md", ".part.md", ".partial.md", "~")):
        return True
    return False


def _is_ignored_entry(name: str) -> bool:
    if is_staging_entry(name) or name == "_hub":
        return True
    return not MESSAGE_SHAPED_RE.fullmatch(name)


def _stat_signature(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _BoardIdentityError(OSError):
    pass


def _root_identity(info: os.stat_result) -> Tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _single_scan_pass(
    board_dir: Path,
    expected_root_id: Optional[Tuple[int, int]] = None,
) -> Tuple[Dict[str, SnapshotEntry], Dict[str, UnsafeEntry], Tuple[int, int]]:
    entries: Dict[str, SnapshotEntry] = {}
    unsafe_entries: Dict[str, UnsafeEntry] = {}

    dir_fd = os.open(board_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        dir_info = os.fstat(dir_fd)
        if not stat.S_ISDIR(dir_info.st_mode):
            raise _BoardIdentityError("Board path is not a directory")
        root_id = _root_identity(dir_info)
        if expected_root_id is not None and root_id != expected_root_id:
            raise _BoardIdentityError("Board root identity changed")
        names = sorted(os.listdir(dir_fd))
        for name in names:
            if _is_ignored_entry(name):
                continue
            try:
                linfo = os.lstat(name, dir_fd=dir_fd)
            except FileNotFoundError as exc:
                raise OSError("Entry vanished during scan") from exc
            if stat.S_ISLNK(linfo.st_mode):
                unsafe_entries[name] = UnsafeEntry(name, "symlink")
                continue
            if not stat.S_ISREG(linfo.st_mode):
                unsafe_entries[name] = UnsafeEntry(name, "non_regular")
                continue

            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    unsafe_entries[name] = UnsafeEntry(name, "non_regular")
                    continue
                if _stat_signature(before) != _stat_signature(linfo):
                    raise OSError("Entry changed between lstat and open")

                is_v2_cand = is_v2_candidate_filename(name)
                cap = MAX_V2_FILE_BYTES if is_v2_cand else MAX_LEGACY_FILE_BYTES
                chunks: List[bytes] = []
                total = 0
                while total <= cap:
                    chunk = os.read(fd, min(65536, cap + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                raw = b"".join(chunks)

                after = os.fstat(fd)
                if _stat_signature(before) != _stat_signature(after):
                    raise OSError("Entry modified during read")
            finally:
                os.close(fd)

            linfo_after = os.lstat(name, dir_fd=dir_fd)
            if _stat_signature(linfo_after) != _stat_signature(before):
                raise OSError("Entry replaced during read")

            digest = hashlib.sha256(raw).hexdigest()
            entries[name] = SnapshotEntry(
                filename=name,
                sha256=digest,
                raw_bytes=raw,
                size=before.st_size,
                mtime_ns=before.st_mtime_ns,
                oversize_legacy=(not is_v2_cand and len(raw) > cap),
            )
        dir_info_end = os.fstat(dir_fd)
        if _root_identity(dir_info_end) != root_id:
            raise _BoardIdentityError("Board root identity changed during scan")
    finally:
        os.close(dir_fd)
    return entries, unsafe_entries, root_id


def scan_board(board_dir: Path, _second_pass_hook=None) -> BoardSnapshot:
    """Enumerate board_dir safely and compare two full inventories of names and hashes."""
    board_path = Path(board_dir)
    try:
        info = board_path.lstat()
    except OSError:
        return BoardSnapshot(healthy=False, board_dir=str(board_path), scan_error="board_unavailable")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return BoardSnapshot(healthy=False, board_dir=str(board_path), scan_error="board_not_directory")

    expected_root_id = _root_identity(info)
    try:
        entries_1, unsafe_1, id_1 = _single_scan_pass(board_path, expected_root_id=expected_root_id)
        if _second_pass_hook is not None:
            _second_pass_hook()
        entries_2, unsafe_2, id_2 = _single_scan_pass(board_path, expected_root_id=expected_root_id)
        info_end = board_path.lstat()
        if stat.S_ISLNK(info_end.st_mode) or not stat.S_ISDIR(info_end.st_mode):
            return BoardSnapshot(healthy=False, board_dir=str(board_path), scan_error="board_not_directory")
        if id_1 != expected_root_id or id_2 != expected_root_id or _root_identity(info_end) != expected_root_id:
            return BoardSnapshot(healthy=False, board_dir=str(board_path), scan_error="board_identity_changed")
    except _BoardIdentityError:
        return BoardSnapshot(healthy=False, board_dir=str(board_path), scan_error="board_identity_changed")
    except OSError:
        return BoardSnapshot(healthy=False, board_dir=str(board_path), scan_error="scan_io_or_race")

    inv_1 = (
        {k: (v.sha256, v.size, v.mtime_ns) for k, v in entries_1.items()},
        {k: v.reason for k, v in unsafe_1.items()},
    )
    inv_2 = (
        {k: (v.sha256, v.size, v.mtime_ns) for k, v in entries_2.items()},
        {k: v.reason for k, v in unsafe_2.items()},
    )
    if inv_1 != inv_2:
        return BoardSnapshot(healthy=False, board_dir=str(board_path), scan_error="scan_unstable")

    return BoardSnapshot(
        healthy=True,
        board_dir=str(board_path),
        entries=entries_2,
        unsafe_entries=unsafe_2,
        scan_error=None,
        root_dev=expected_root_id[0],
        root_ino=expected_root_id[1],
    )


# --- CLI entry point ---

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Lint BoardHub messages (v2 strict + legacy tolerant) without modifying the board."
    )
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
        help="Optional runtime manifest JSON path for immutability checks.",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Optional UTC RFC 3339 timestamp for deterministic evaluation.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON lint report.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        board_dir, _runtime_dir, _human_role = resolve_cli_settings(args)
    except (SecurityError, FormatError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.as_of is not None:
        as_of = parse_utc_rfc3339(args.as_of)
        if as_of is None:
            print("Invalid --as-of UTC RFC 3339 timestamp.", file=sys.stderr)
            return 2
    else:
        as_of = datetime.now(timezone.utc)

    runtime_corrupt = False
    manifest = None
    if args.manifest is not None:
        try:
            manifest = read_private_json_file(args.manifest)
            if manifest is None:
                manifest = {"version": 2, "entries": {}}
        except (SecurityError, FormatError, OSError):
            runtime_corrupt = True
            manifest = {"version": 2, "entries": {}}

    snapshot = scan_board(board_dir)
    board_state = load_sibling_module("board_state")
    result = board_state.rebuild(
        snapshot,
        manifest if manifest is not None else {},
        as_of,
        runtime_corrupt=runtime_corrupt,
    )
    lint_report = result["lint"]
    exit_code = lint_report["exit_code"]

    if args.json:
        print(json.dumps(lint_report, indent=2, ensure_ascii=False))
    else:
        counts = lint_report["counts"]
        print(
            f"board_lint: exit={exit_code} snapshot={lint_report['snapshot_id']} "
            f"high={counts['high']} error={counts['error']} warning={counts['warning']}"
        )
        for item in lint_report["diagnostics"]:
            msg_part = f" msg={item['message_id']}" if item["message_id"] else ""
            thr_part = f" thread={item['thread_id']}" if item["thread_id"] else ""
            print(
                f"  [{item['severity']}] {item['code']} file={escape_safe_text(item['file'])}"
                f"{msg_part}{thr_part}: {item['message']}"
            )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
