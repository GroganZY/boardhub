#!/usr/bin/env python3
"""Read the visible conversation from explicitly selected Claude Desktop sessions."""

import argparse
from bisect import bisect_left
from collections import defaultdict, deque
from datetime import datetime
import hashlib
from html import unescape
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import NamedTuple, Optional


UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
DESKTOP_ID = re.compile(r"local_" + UUID)
CLI_ID = re.compile(UUID)
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
PROJECTS = (".claude", "projects")
CWD_FIELDS = ("cwd", "originCwd", "gitAnchorsFolderRealpath")
CONFIG_LIMIT = 16 * 1024
SCAN_LIMIT = 8 * 1024 * 1024
LINE_LIMIT = 1024 * 1024
TEXT_LIMIT = 4096
ITEM_LIMIT = 400
REF_LIMIT = 64
ANNOTATION_LIMIT = 64
ARTIFACT_INDEX_LIMIT = 512

CROSS_SESSION = {"cross-session-message", "session-message", "inter-session-message", "teammate-message"}
WRAPPER_NAMES = (
    r"system-reminder|local-command(?:-[\w-]+)?|slash-command|command-(?:name|message|args)|"
    r"user-prompt-submit-hook|task-notification|tool[-_]result|attachments?|pasted[-_](?:text|block)|"
    + "|".join(sorted(CROSS_SESSION))
)
TAG_ATTRS = r"""(?P<attrs>(?:[^<>"']|"[^"\r\n]*"|'[^'\r\n]*')*)"""
WRAPPER_TAG = r"<(?P<close>/)?(?P<tag>{names})(?=[\s/>]|$)" + TAG_ATTRS + r"""(?:>|(?=[<>"']|$))"""
WRAPPER = re.compile(WRAPPER_TAG.format(names=WRAPPER_NAMES), re.IGNORECASE)
USER_WRAPPER = re.compile(
    WRAPPER_TAG.format(names=WRAPPER_NAMES + "|artifact-view-context|artifact-content-authored-by-others"),
    re.IGNORECASE,
)
USER_WRAPPER_OR_CODE = re.compile(
    USER_WRAPPER.pattern + r"|(?<!`)(?P<ticks>`+)(?!`)[^`\n]*(?<!`)(?P=ticks)(?!`)"
    r"|(?P<image>^\[Image: source: [^\]\n]+\]$)",
    re.IGNORECASE | re.MULTILINE,
)
HYPHENATED_TAG = re.compile(r"<(?P<close>/)?(?P<name>[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+)(?=[\s/>])" + TAG_ATTRS + ">")
LEADING_TAG = re.compile(r"\s*" + HYPHENATED_TAG.pattern)
USER_MARKUP = {
    "annotation-xml", "color-profile", "font-face", "font-face-format", "font-face-name",
    "font-face-src", "font-face-uri", "missing-glyph",
    "custom-element", "custom-tag", "my-component", "my-tag", "web-component",
}
ATTRIBUTE = re.compile(r'''([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')''')
NOTICE_FIELD = re.compile(r"<(?P<key>summary|status|from-name|message)>(?P<value>.*?)</(?P=key)>", re.DOTALL)
COMMAND_NAME = r"[A-Za-z0-9][\w:-]{0,79}"
SLASH_COMMAND = re.compile(rf"^/(?P<name>{COMMAND_NAME})(?=\s|$)")
COMMAND_WRAPPERS = ("command-", "local-command", "slash-command")
ARTIFACT_TOOL = re.compile(r"artifact.*(?:publish|read|get|create|update)|(?:publish|read|get|create|update).*artifact", re.I)
ARTIFACT_URL = re.compile(r"https?://[^\s()<>\"']+/artifacts?/(?P<id>[\w-]+)[^\s()<>\"']*", re.I)
ARTIFACT_LINK = re.compile(r"\[(?P<title>[^\]\n]+)\]\((?P<url>https?://[^\s)]+)\)")
PASTE_MARKER = re.compile(r"\[Pasted text[^\]\r\n]*\]", re.IGNORECASE)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")
BOARD_MENTION = re.compile(
    rf"(?<![\w-])(?P<uuid>{UUID})(?![\w-])|"
    r"(?<!\w)(?:#|\bboard\s+|消息\s*)(?P<number>[0-9]+)(?!\w)|"
    r"(?<!\w)(?P<filename>[0-9]{4,})(?=_[\w-]+\.md\b)|"
    r"(?<!\w)(?P<padded>0[0-9]{3,})(?!\w)", re.IGNORECASE,
)


class Entry(NamedTuple):
    id: str
    role: str
    at: datetime
    text: str
    folded: bool
    start: Optional[datetime] = None
    annotations: tuple = ()


def _allowlist(config):
    if not isinstance(config, dict) or set(config) != {"sessions"}:
        return {}
    sessions = config["sessions"]
    if not isinstance(sessions, list) or len(sessions) > 8:
        return {}
    selected = {}
    labels = set()
    for entry in sessions:
        if not isinstance(entry, dict) or set(entry) != {"label", "match"}:
            return {}
        label, match = entry["label"], entry["match"]
        if (not isinstance(label, str) or not LABEL.fullmatch(label) or label in labels
                or not isinstance(match, dict) or set(match) - {"desktop_id", "cli_session_id", "cwd"}):
            return {}
        desktop, cli, cwd = (match.get(name) for name in ("desktop_id", "cli_session_id", "cwd"))
        if (not isinstance(desktop, str) or not DESKTOP_ID.fullmatch(desktop) or desktop in selected
                or ("cli_session_id" in match and (not isinstance(cli, str) or not CLI_ID.fullmatch(cli)))
                or ("cwd" in match and not _usable_cwd(cwd))):
            return {}
        selected[desktop] = (label, cli, os.path.normpath(cwd) if cwd else None)
        labels.add(label)
    return selected


def _usable_cwd(value):
    return isinstance(value, str) and len(value) <= 4096 and os.path.isabs(value) and "\0" not in value


def _locations(fields):
    return dict.fromkeys(os.path.normpath(fields[key]) for key in CWD_FIELDS if _usable_cwd(fields.get(key)))


def _project_name(cwd):
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


def _owned_file(info):
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1


def _transcript_info(discovery, projects, project, cli):
    with discovery._directory(projects, project) as (directory, _):
        if directory is not None:
            try:
                info = os.stat(cli + ".jsonl", dir_fd=directory, follow_symlinks=False)
                if _owned_file(info):
                    return info
            except OSError:
                pass
    return None


def candidates(home, discovery):
    """Enumerate desktop metadata and stat exact derived paths without opening any transcript."""
    rows = []
    with discovery._directory(None, home) as (root, _):
        with discovery._under(root, PROJECTS) as projects:
            metadata = discovery.claude_desktop_metadata(discovery._FileCache(), root)
            for desktop, fields, mtime in metadata:
                cli = fields.get("cliSessionId")
                cli = cli if isinstance(cli, str) and CLI_ID.fullmatch(cli) else None
                seen = discovery.claude_metadata_seen(fields, mtime)
                locations = []
                for cwd in _locations(fields):
                    project = _project_name(cwd)
                    exists = bool(cli and projects is not None and _transcript_info(discovery, projects, project, cli))
                    locations.append({"cwd": cwd, "project": project, "transcript_exists": exists})
                rows.append({
                    "desktop_id": desktop, "cli_session_id": cli,
                    "title": discovery._title(fields.get("title") or fields.get("name")),
                    "last_seen": seen.isoformat().replace("+00:00", "Z") if seen else None,
                    "archived": fields.get("isArchived") is True or discovery._closed(fields.get("status")),
                    "locations": locations,
                })
    rows.sort(key=lambda row: (row["last_seen"] or "", row["desktop_id"]), reverse=True)
    return rows


def _leading_wrapper_end(text, cursor):
    opening = LEADING_TAG.match(text, cursor)
    if (not opening or opening["close"] or opening["name"] in USER_MARKUP
            or USER_WRAPPER.match(text, opening.start("name") - 1)):
        return None
    if opening["attrs"].rstrip().endswith("/"):
        return opening.end()
    # Count only matching tags. The payload can contain arbitrary markup or JSON.
    tag = opening["name"]
    depth = 1
    for match in HYPHENATED_TAG.finditer(text, opening.end()):
        if match["name"] != tag:
            continue
        if match["close"]:
            depth -= 1
            if not depth:
                return match.end()
        elif not match["attrs"].rstrip().endswith("/"):
            depth += 1
    return None


def _strip_wrappers(text, *, user=False):
    parts, contexts, stack = [], [], []
    wrappers = USER_WRAPPER if user else WRAPPER
    top_level = USER_WRAPPER_OR_CODE if user else WRAPPER
    cursor = 0
    leading = user
    while cursor < len(text):
        if leading and not stack:
            end = _leading_wrapper_end(text, cursor)
            if end is not None:
                cursor = end
                continue
        pattern = wrappers if stack else top_level
        match = pattern.search(text, cursor)
        if match is None:
            break
        if not stack:
            prefix = text[cursor:match.start()]
            parts.append(prefix)
            leading = leading and not prefix.strip()
        cursor = match.end()
        if match["tag"] is None:
            if match["image"]:
                contexts.append((match.start(), "image", "", ""))
            else:
                parts.append(match[0])
            leading = False
            continue
        tag = match["tag"].lower()
        # Most payloads are opaque. System reminders can carry separate notices.
        if user and stack and stack[-1][0] not in ("system-reminder", tag):
            continue
        if match["close"]:
            if stack and stack[-1][0] == tag:
                _, opening, capture = stack.pop()
                if capture:
                    contexts.append((opening.start(), tag, opening["attrs"], text[opening.end():match.start()]))
        else:
            capture = user and (not stack or stack[-1][0] == "system-reminder") and (
                tag in ("artifact-view-context", "task-notification", "command-name", "command-message", "slash-command")
                or tag in CROSS_SESSION or tag.startswith("local-command"))
            if match["attrs"].rstrip().endswith("/"):
                if capture:
                    contexts.append((match.start(), tag, match["attrs"], ""))
            else:
                stack.append((tag, match, capture))
    if not stack:
        parts.append(text[cursor:])
    return "".join(parts), contexts


def _collapse_pastes(text):
    lines = []
    fence = None
    for line in text.split("\n"):
        marker = FENCE.fullmatch(line)
        if fence is None:
            if marker:
                fence = marker[1]
                lines.append("[粘贴内容已折叠]")
            else:
                lines.append(PASTE_MARKER.sub("[粘贴内容已折叠]", line))
        elif marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
            fence = None
    return "\n".join(lines)


def _bounded_text(text, limit=TEXT_LIMIT):
    text = text.strip()
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return raw.decode("utf-8"), False
    suffix = "\n\n[…]" if limit >= 7 else ""
    prefix = raw[:limit - len(suffix.encode("utf-8"))].decode("utf-8", errors="ignore")
    return prefix.rstrip() + suffix, True


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _visible(blocks):
    parts, images = [], []
    position = 0
    for block in blocks:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text = block["text"].replace("\r\n", "\n").replace("\r", "\n")
            parts.append(text)
            position += len(text) + 2
        elif block.get("type") == "image":
            # Order an image between the surrounding text blocks.
            images.append((position - 1, "image", "", ""))
    return "\n\n".join(parts), images


def _first_text(fields, *names):
    for name in names:
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _json_value(text):
    if text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            pass
    return None


def _short_label(text, limit=80):
    line = " ".join(text.split()).encode("utf-8", errors="replace").decode("utf-8")
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _artifact_title(title):
    title = title.strip()
    return _short_label(title, 120) if title and not title.startswith("{") and _json_value(title) is None else ""


def _artifact_keys(*values):
    for value in values:
        if not value or len(value) > 512 or any(char.isspace() for char in value):
            continue
        key = value.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        if key:
            yield key
        url = ARTIFACT_URL.fullmatch(key)
        if url:
            yield url["id"]


def _artifact_titles(value, artifact_tool=False, fallback="", depth=0):
    if depth > 6:
        return
    if isinstance(value, str):
        decoded = _json_value(value)
        if decoded is not None:
            yield from _artifact_titles(decoded, artifact_tool, fallback, depth + 1)
            return
        linked = False
        for link in ARTIFACT_LINK.finditer(value):
            if ARTIFACT_URL.fullmatch(link["url"]):
                yield link["url"], link["title"]
                linked = True
        if linked:
            return
        urls = {match[0].rstrip(".,;]") for match in ARTIFACT_URL.finditer(value)}
        title = re.search(r"^[ \t]*(?:Artifact[ \t]+)?Title:[ \t]*(.+)$", value, re.I | re.M)
        if len(urls) == 1:
            yield urls.pop(), title[1] if title else fallback
        artifact_id = re.search(r"^[ \t]*artifact[-_ ]?id:[ \t]*([\w-]+)[ \t]*$", value, re.I | re.M)
        if artifact_id:
            yield artifact_id[1], title[1] if title else fallback
    elif isinstance(value, list):
        for entry in value:
            yield from _artifact_titles(entry, artifact_tool, fallback, depth + 1)
    elif isinstance(value, dict):
        artifact_id = _first_text(value, "artifactId", "artifact_id", "artifact-id", "artifact")
        url = _first_text(value, "url", "href")
        artifact_url = _first_text(value, "artifactUrl", "artifact_url", "artifact-url")
        title = _first_text(value, "title", "artifactTitle", "artifact_title", "name") or fallback
        if artifact_tool or artifact_id or artifact_url or ARTIFACT_URL.fullmatch(url) or value.get("type") == "artifact":
            for key in (artifact_id or _first_text(value, "id"), artifact_url or url):
                if key:
                    yield key, title
        for name in ("artifact", "artifacts", "result", "data", "structuredContent", "content", "text"):
            if name in value:
                nested_artifact = name in ("artifact", "artifacts")
                yield from _artifact_titles(value[name], artifact_tool or nested_artifact,
                                            title if nested_artifact else fallback, depth + 1)


def _remember(index, key, value):
    index.pop(key, None)
    index[key] = value
    if len(index) > ARTIFACT_INDEX_LIMIT:
        del index[next(iter(index))]


def _notice_fields(body):
    decoded = _json_value(body)
    if isinstance(decoded, dict):
        return decoded
    return {match["key"]: unescape(match["value"]) for match in NOTICE_FIELD.finditer(body)}


def _context_annotation(tag, attrs, body, titles):
    if tag == "image":
        return {"kind": "image", "label": "图片"}
    attrs = {match[1].lower(): unescape(match[2] if match[2] is not None else match[3])
             for match in ATTRIBUTE.finditer(attrs)}
    if tag == "artifact-view-context":
        keys = _artifact_keys(_first_text(attrs, "artifact", "artifact-id", "artifactid", "artifact_id", "id"),
                              _first_text(attrs, "artifact-url", "artifacturl", "artifact_url", "url"))
        title = _artifact_title(_first_text(attrs, "title", "artifact-title"))
        title = title or next((titles[key] for key in keys if key in titles), "")
        label = f"当时在看：{title}" if title else "当时在看一个 artifact 页面"
        return {"kind": "viewing", "label": label}
    if tag == "task-notification":
        fields = {**attrs, **_notice_fields(body)}
        status = _first_text(fields, "status").lower().replace("-", "_")
        failed = status in {"failed", "failure", "error", "cancelled", "canceled", "killed", "aborted",
                            "timeout", "timed_out", "interrupted"}
        summary = _first_text(fields, "summary") or "未提供摘要"
        outcome = "失败" if failed else "完成"
        return {"kind": "task", "label": _short_label(f"后台任务{outcome}：{summary}")}
    if tag in CROSS_SESSION:
        fields = {**attrs, **_notice_fields(body)}
        sender = _first_text(fields, "from-name", "from_name", "fromName", "fromname", "sender-name", "sender_name",
                             "from", "sender", "from-session", "from_session", "sender-id", "sender_id",
                             "session", "session-id", "session_id", "teammate_id", "agent_id")
        sender = _short_label(sender or "其他会话", 64)
        detail = _first_text(fields, "message", "text") or body.strip()
        return {"kind": "peer", "label": f"来自「{sender}」的消息", "detail": detail}
    name = _first_text(attrs, "name", "command", "command-name", "command_name")
    if not name and tag in ("command-name", "command-message", "local-command-name", "local-command", "slash-command"):
        name = body.strip()
    name = name.removeprefix("/")
    if re.fullmatch(COMMAND_NAME, name):
        return {"kind": "command", "label": f"执行了命令 /{name}"}
    return None


def _annotations(contexts, titles):
    annotations, commands = [], set()
    detail_budget = TEXT_LIMIT
    # Slash commands normally carry both a canonical name and a message wrapper.
    has_command_name = any(tag in ("command-name", "local-command-name") for _, tag, _, _ in contexts)
    for _, tag, attrs, body in sorted(contexts, key=lambda context: context[0]):
        if has_command_name and tag == "command-message":
            continue
        annotation = _context_annotation(tag, attrs, body, titles)
        if not annotation:
            continue
        if annotation["kind"] == "command":
            if annotation["label"] in commands:
                continue
            commands.add(annotation["label"])
        if "detail" in annotation:
            detail = annotation.pop("detail")
            if detail_budget:
                annotation["detail"], _ = _bounded_text(detail, detail_budget)
                detail_budget -= len(annotation["detail"].encode("utf-8"))
        annotations.append(annotation)
        if len(annotations) == ANNOTATION_LIMIT:
            break
    return tuple(annotations)


class TranscriptTail:
    def __init__(self):
        self.signature = None
        self.offset = 0
        self.skipping = False
        self.anchor = b""
        self.history = deque(maxlen=ITEM_LIMIT)
        self.artifact_titles = {}
        self.artifact_tools = {}
        self.start = self.answer = self.group = None
        self.tool_group = self.compacted = False

    def items(self):
        yield from self.history
        if self.answer is not None:
            yield self.answer

    def _finish_turn(self, start):
        if self.answer is not None:
            self.history.append(self.answer)
        self.start, self.answer, self.group = start, None, None
        self.tool_group = False

    def _remember_artifact_results(self, record, blocks):
        tool_id = record.get("sourceToolUseID")
        for block in blocks:
            if block.get("type") != "tool_result" or block.get("is_error"):
                continue
            self._remember_artifacts(block.get("content"), block.get("tool_use_id"))
            tool_id = block.get("tool_use_id") or tool_id
        if "toolUseResult" in record and not any(block.get("is_error") for block in blocks):
            self._remember_artifacts(record["toolUseResult"], tool_id)

    def _remember_artifacts(self, value, tool_id):
        known_tool = isinstance(tool_id, str) and tool_id in self.artifact_tools
        fallback = self.artifact_tools[tool_id] if known_tool else ""
        for artifact, title in _artifact_titles(value, known_tool, fallback):
            title = _artifact_title(title)
            if title:
                for key in _artifact_keys(artifact):
                    _remember(self.artifact_titles, key, title)

    def _ingest(self, raw, cli, offset, discovery):
        try:
            record = json.loads(raw)
        except (ValueError, RecursionError):
            return
        if not isinstance(record, dict) or record.get("isSidechain") or record.get("isVirtual"):
            return
        if record.get("sessionId") not in (None, cli):
            return
        at = discovery._timestamp(record.get("timestamp"))
        if at is None:
            return
        record_id = record.get("uuid")
        identity = record_id if isinstance(record_id, str) and CLI_ID.fullmatch(record_id) else offset

        def item(role, text, folded=False, start=None, annotations=()):
            item_id = hashlib.sha256(f"{cli}:{identity}:{role}:".encode()).hexdigest()[:24]
            visible, truncated = _bounded_text(text)
            return Entry(item_id, role, at, visible, folded or truncated, start, annotations)

        kind = record.get("type")
        if ((kind in ("system", "summary") and record.get("subtype") == "compact_boundary")
                or (kind == "user" and record.get("isCompactSummary") is True)):
            if not self.compacted:
                self._finish_turn(None)
                self.history.append(item("event", "上下文已压缩", folded=True))
                self.compacted = True
            return
        if kind == "system" and record.get("subtype") == "local_command":
            source = record.get("content")
            if isinstance(source, str):
                _, contexts = _strip_wrappers(source, user=True)
                annotations = _annotations(contexts, self.artifact_titles)
                if annotations:
                    self.history.append(item("event", "", annotations=annotations))
            return
        message = record.get("message")
        if kind not in ("user", "assistant") or not isinstance(message, dict) or message.get("role") != kind:
            return
        blocks = _blocks(message.get("content"))
        if kind == "user":
            if (any(key in record for key in ("toolUseResult", "sourceToolAssistantUUID", "sourceToolUseID"))
                    or any(block.get("type") == "tool_result" for block in blocks)):
                self._remember_artifact_results(record, blocks)
                return
            annotations_only = any(record.get(key) for key in ("isMeta", "isSynthetic", "isVisibleInTranscriptOnly"))
            source, images = _visible(blocks)
            text, contexts = _strip_wrappers(source, user=True)
            text = "" if annotations_only else _collapse_pastes(text).strip()
            wrapped_command = any(tag.startswith(COMMAND_WRAPPERS) for _, tag, _, _ in contexts)
            command = None if wrapped_command else SLASH_COMMAND.match(text)
            if command:
                contexts.append((len(source), "command-name", "", command["name"]))
                text = text[command.end():].strip()
            annotations = _annotations(contexts + images, self.artifact_titles)
            if text or annotations:
                if not annotations_only and (text or any(a["kind"] not in ("task", "peer") for a in annotations)):
                    self._finish_turn(at)
                    self.compacted = False
                self.history.append(item("user", text, text == "[粘贴内容已折叠]", annotations=annotations))
            return

        if record.get("isApiErrorMessage") or record.get("isMeta") or message.get("model") == "<synthetic>":
            return
        group = message.get("id")
        group = group if isinstance(group, str) and group else identity
        same_group = group == self.group
        if not same_group:
            self.group, self.tool_group = group, False
        self.start = self.start or at
        if (message.get("stop_reason") == "tool_use"
                or any(block.get("type") in ("tool_use", "server_tool_use") for block in blocks)):
            for block in blocks:
                name, tool_id = block.get("name"), block.get("id")
                if (block.get("type") in ("tool_use", "server_tool_use") and isinstance(name, str)
                        and ARTIFACT_TOOL.search(name) and isinstance(tool_id, str) and len(tool_id) <= 128):
                    payload = block.get("input")
                    title = _first_text(payload, "title", "artifactTitle", "artifact_title") if isinstance(payload, dict) else ""
                    _remember(self.artifact_tools, tool_id, _artifact_title(title))
            self.answer = None
            self.tool_group = True
        if self.tool_group:
            return
        source, images = _visible(blocks)
        text, _ = _strip_wrappers(source)
        text = text.strip()
        annotations = _annotations(images, self.artifact_titles)
        if text or annotations:
            if same_group and self.answer:
                if text and self.answer.text and not text.startswith(self.answer.text):
                    text = self.answer.text + "\n\n" + text
                text = text or self.answer.text
                annotations = annotations or self.answer.annotations
            self.answer = item("assistant", text, start=min(self.start, at), annotations=annotations)
            self.compacted = False

    def refresh(self, directory, name, info, cli, discovery):
        signature = discovery._signature(info)
        if signature == self.signature:
            return True
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            with os.fdopen(os.open(name, flags, dir_fd=directory), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not _owned_file(opened) or discovery._signature(opened) != signature:
                    return False
                size = opened.st_size
                prior = self.signature
                if (prior is None or prior[:2] != signature[:2] or size <= prior[2]
                        or size - self.offset > SCAN_LIMIT
                        or os.pread(stream.fileno(), len(self.anchor), prior[2] - len(self.anchor)) != self.anchor):
                    self.__init__()
                    self.offset = max(0, size - SCAN_LIMIT)
                    self.skipping = bool(self.offset and os.pread(stream.fileno(), 1, self.offset - 1) != b"\n")
                stream.seek(self.offset)
                while self.offset < size:
                    start = self.offset
                    raw = stream.readline(min(size - start, LINE_LIMIT + 1))
                    if not raw:
                        return False
                    complete = raw.endswith(b"\n")
                    if not complete and not self.skipping and len(raw) <= LINE_LIMIT:
                        break
                    if complete and not self.skipping and len(raw) <= LINE_LIMIT:
                        self._ingest(raw, cli, start, discovery)
                    self.offset += len(raw)
                    self.skipping = not complete
                anchor_size = min(128, size)
                self.anchor = os.pread(stream.fileno(), anchor_size, size - anchor_size)
                after = os.fstat(stream.fileno())
                if ((opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino) or after.st_size < size
                        or not _owned_file(after)
                        or (after.st_size == size and discovery._signature(after) != signature)):
                    return False
        except OSError:
            return False
        self.signature = signature
        return True


class BoardReferences:
    def __init__(self, messages=(), dispatches=()):
        self.by_id, self.by_number = {}, {}
        events = set()
        for row in messages:
            message_id = row["id"]
            if CLI_ID.fullmatch(message_id):
                self.by_id[message_id.lower()] = message_id
                self.by_number[row["number"].lstrip("0") or "0"] = message_id
                events.add((row["at"], message_id))
        ids = set(self.by_id.values())
        events.update((at, message_id) for at, message_id in dispatches if message_id in ids)
        self.events = sorted((datetime.fromisoformat(at.replace("Z", "+00:00")), message_id)
                             for at, message_id in events)

    def links(self, entry):
        refs = {}
        if entry.role != "event":
            for mention in BOARD_MENTION.finditer(entry.text):
                number = mention["number"] or mention["filename"] or mention["padded"]
                uuid = mention["uuid"]
                link = (self.by_id.get(uuid.lower()) if uuid else
                        self.by_number.get(number.lstrip("0") or "0"))
                if link:
                    refs[link] = None
        if entry.start is not None:
            index = bisect_left(self.events, (entry.start, ""))
            while index < len(self.events) and self.events[index][0] <= entry.at and len(refs) < REF_LIMIT:
                refs[self.events[index][1]] = None
                index += 1
        return list(refs)[:REF_LIMIT]


class ClaudeTranscripts:
    def __init__(self, discovery, home=None):
        self.discovery = discovery
        self.home = Path.home() if home is None else Path(home)
        self.metadata = discovery._FileCache()
        self.tails = {}
        self.entries = []
        self.references = None
        self.view = {"items": []}

    def _resolve(self, root, projects, selected):
        matches = defaultdict(set)
        for desktop, fields, _ in self.discovery.claude_desktop_metadata(self.metadata, root, selected):
            _, cli_pin, cwd_pin = selected[desktop]
            cli = fields.get("cliSessionId")
            if not isinstance(cli, str) or not CLI_ID.fullmatch(cli) or (cli_pin and cli_pin != cli):
                continue
            for cwd in _locations(fields):
                if not cwd_pin or cwd == cwd_pin:
                    matches[desktop].add((_project_name(cwd), cli))
        for desktop, possibilities in matches.items():
            # Conflicting CLI mappings require a pin even when one transcript is absent.
            if len({cli for _, cli in possibilities}) != 1:
                continue
            existing = []
            for project, cli in sorted(possibilities):
                info = _transcript_info(self.discovery, projects, project, cli)
                if info is not None:
                    existing.append((project, cli, info))
            if len(existing) == 1:
                yield desktop, existing[0]

    def read(self, config, references, since=None):
        selected = _allowlist(config)
        tails, merged = {}, []
        if selected:
            discovery = self.discovery
            with discovery._directory(None, self.home) as (root, _):
                with discovery._under(root, PROJECTS) as projects:
                    if projects is not None:
                        resolved = list(self._resolve(root, projects, selected))
                        for desktop, (project, cli, info) in resolved:
                            key = project, cli
                            if key in tails:
                                continue
                            tail = self.tails.get(key) or TranscriptTail()
                            with discovery._directory(projects, project) as (directory, _):
                                if directory is None or not tail.refresh(directory, cli + ".jsonl", info, cli, discovery):
                                    continue
                            tails[key] = tail
                            merged.extend((entry, selected[desktop][0]) for entry in tail.items()
                                          if since is None or entry.at >= since)
        self.tails = tails
        merged.sort(key=lambda pair: pair[0].at)
        merged = merged[-ITEM_LIMIT:]
        if merged == self.entries and references is self.references:
            return self.view
        self.entries, self.references = merged, references
        self.view = {"items": [
            {"id": item.id, "session_label": label, "role": item.role,
             "ts": item.at.isoformat().replace("+00:00", "Z"), "text": item.text,
             "folded_default": item.folded, "refs": references.links(item),
             **({"annotations": list(item.annotations)} if item.annotations else {})}
            for item, label in merged
        ]}
        return self.view


def main():
    parser = argparse.ArgumentParser(description="List Claude Desktop transcript candidates using metadata only.")
    parser.add_argument("--candidates", action="store_true", required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args()
    # Isolated mode removes the script directory from imports.
    path = Path(__file__).resolve().with_name("session_discovery.py")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not _owned_file(info) or info.st_mode & 0o022:
            parser.error("Unsafe sibling session_discovery.py")
    finally:
        os.close(fd)
    spec = importlib.util.spec_from_file_location("session_discovery", path)
    discovery = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(discovery)
    print(json.dumps(candidates(args.home, discovery), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
