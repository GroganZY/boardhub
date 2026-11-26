#!/usr/bin/env python3
"""Build the offline BoardHub v2 chat view with dashboard header.

Python 3.8+ standard library only.
Uses the single board_lint + board_state parser and validator.
"""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import html
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Dict, Optional
from urllib.parse import urlsplit

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


DISPLAY_TZ = timezone.utc
DISPLAY_TZ_LABEL = "UTC"
LIST_ITEM = re.compile(r"^( *)([-+*]|\d+[.)])\s+(.*)$")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
INLINE = re.compile(
    r"(`+)(.+?)\1(?!`)"
    r"|\[([^\]\n]+)\]\(([^\s()]+(?:\([^\s()]*\)[^\s()]*)*)\)"
    r"|\*\*(.+?)\*\*|__(.+?)__|\\([\\`*{}\[\]()#+.!_|>~-])",
    re.DOTALL,
)

COLORS = {
    "claude": "#bd542d",
    "claude_main": "#bd542d",
    "claude_side": "#9a428a",
    "dsh": "#187c65",
    "codex": "#466bc1",
    "gpt": "#7956c0",
    "gemini": "#1d7a9c",
}


def escape(value) -> str:
    return html.escape(str(value), quote=True)


def sender_color(sender: str) -> str:
    if sender in COLORS:
        return COLORS[sender]
    if sender == board_lint.HUMAN_ROLE:
        return "#8c5812"
    hue = int(hashlib.sha256(sender.encode("utf-8")).hexdigest()[:8], 16) % 360
    return f"hsl({hue} 48% 42%)"


def safe_url(url: str) -> bool:
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    if "\\" in url or url.startswith("//"):
        return False
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return False
    return scheme in ("", "https", "http", "mailto")


def inline(text: str, depth: int = 0) -> str:
    """Escape every source fragment; only this renderer can emit HTML tags."""
    if depth > 10:
        return escape(text)
    result, pos = [], 0
    for match in INLINE.finditer(text):
        result.append(escape(text[pos:match.start()]))
        code_mark, code, label, url, bold, bold_under, literal = match.groups()
        if code_mark:
            result.append("<code>" + escape(code) + "</code>")
        elif label is not None:
            if safe_url(url):
                result.append('<a href="{}" rel="noreferrer noopener">{}</a>'.format(
                    escape(url), inline(label, depth + 1)))
            else:
                result.append(escape(match[0]))
        elif literal is not None:
            result.append(escape(literal))
        else:
            result.append("<strong>" + inline(bold or bold_under, depth + 1) + "</strong>")
        pos = match.end()
    result.append(escape(text[pos:]))
    return "".join(result)


def table_cells(line: str):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells, current, code_ticks, i = [], [], 0, 0
    while i < len(line):
        char = line[i]
        if char == "\\" and i + 1 < len(line):
            current.append(line[i:i + 2])
            i += 2
            continue
        if char == "`":
            end = i + 1
            while end < len(line) and line[end] == "`":
                end += 1
            count = end - i
            if not code_ticks:
                code_ticks = count
            elif code_ticks == count:
                code_ticks = 0
            current.append(line[i:end])
            i = end
            continue
        if char == "|" and not code_ticks:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        i += 1
    cells.append("".join(current).strip())
    return cells


def table_start(lines, i: int) -> bool:
    if i + 1 >= len(lines) or "|" not in lines[i]:
        return False
    cells = table_cells(lines[i + 1])
    return (
        len(cells) == len(table_cells(lines[i]))
        and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)
    )


def block_start(lines, i: int) -> bool:
    line = lines[i]
    return bool(
        FENCE.match(line)
        or HEADING.match(line)
        or LIST_ITEM.match(line)
        or line.startswith(">")
        or table_start(lines, i)
    )


def render_markdown(text: str, depth: int = 0) -> str:
    if depth > 20:
        return "<pre>" + escape(text) + "</pre>"
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = FENCE.match(line)
        heading = HEADING.match(line)
        item = LIST_ITEM.match(line)
        if fence:
            marker, language = fence.groups()
            code = []
            i += 1
            closing = re.compile(
                r"^\s{0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$"
            )
            while i < len(lines) and not closing.match(lines[i]):
                code.append(lines[i])
                i += 1
            out.append('<pre><code data-language="{}">{}</code></pre>'.format(
                escape(language.strip()), escape("\n".join(code))))
            if i < len(lines):
                i += 1
        elif heading:
            level = len(heading[1])
            out.append("<h{0}>{1}</h{0}>".format(level, inline(heading[2])))
            i += 1
        elif table_start(lines, i):
            headers = table_cells(line)
            alignments = []
            for cell in table_cells(lines[i + 1]):
                alignments.append(
                    "center" if cell.startswith(":") and cell.endswith(":")
                    else "right" if cell.endswith(":")
                    else "left"
                )

            def row(cells, tag):
                cells = (cells + [""] * len(headers))[:len(headers)]
                return "<tr>" + "".join(
                    '<{0} class="align-{1}">{2}</{0}>'.format(tag, alignments[n], inline(c))
                    for n, c in enumerate(cells)
                ) + "</tr>"

            table = ['<div class="table-wrap"><table><thead>', row(headers, "th"), "</thead><tbody>"]
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                table.append(row(table_cells(lines[i]), "td"))
                i += 1
            table.append("</tbody></table></div>")
            out.append("".join(table))
        elif item:
            indent = len(item[1])
            ordered = item[2][0].isdigit()
            tag = "ol" if ordered else "ul"
            start = ' start="{}"'.format(int(item[2][:-1])) if ordered else ""
            parts = ["<" + tag + start + ">"]
            while i < len(lines):
                current = LIST_ITEM.match(lines[i])
                if not current or len(current[1]) != indent or current[2][0].isdigit() != ordered:
                    break
                content_indent = current.start(3)
                content = [current[3]]
                i += 1
                while i < len(lines):
                    following = LIST_ITEM.match(lines[i])
                    if following and len(following[1]) <= indent:
                        break
                    if not lines[i].strip():
                        next_i = i + 1
                        while next_i < len(lines) and not lines[next_i].strip():
                            next_i += 1
                        if next_i == len(lines):
                            i = next_i
                            break
                        next_line = lines[next_i]
                        next_item = LIST_ITEM.match(next_line)
                        if next_item and len(next_item[1]) == indent:
                            i = next_i
                            break
                        if len(next_line) - len(next_line.lstrip()) <= indent:
                            break
                        content.append("")
                        i += 1
                        continue
                    leading = len(lines[i]) - len(lines[i].lstrip())
                    if leading <= indent and block_start(lines, i):
                        break
                    content.append(lines[i][min(leading, content_indent):])
                    i += 1
                parts.append("<li>" + render_markdown("\n".join(content), depth + 1) + "</li>")
            parts.append("</" + tag + ">")
            out.append("".join(parts))
        elif line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(re.sub(r"^> ?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + render_markdown("\n".join(quote), depth + 1) + "</blockquote>")
        else:
            paragraph = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not block_start(lines, i):
                paragraph.append(lines[i])
                i += 1
            out.append("<p>" + "<br>".join(inline(p) for p in paragraph) + "</p>")
    return "\n".join(out)


CSS = r"""
:root{color-scheme:light dark;--bg:#f4f5fa;--card:#fff;--ink:#202536;--muted:#657084;
 --line:#dde1eb;--accent:#6050b8;--flag:#975009;--flag-bg:#fff1d7;--urgent:#ba243a;--ok:#187c65;--ok-bg:#e5f6f1}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.masthead,.dashboard,.toolbar-inner,main{max-width:1120px;margin:auto;padding:20px 24px}
.masthead{display:flex;align-items:center;justify-content:space-between;gap:20px}
.eyebrow{color:var(--accent);font-size:12px;letter-spacing:.14em;font-weight:750}
h1{font-size:26px;line-height:1.3;margin:5px 0}.subtitle{color:var(--muted);font-size:13px}
.dashboard{padding-top:0;display:grid;gap:14px}
.dash-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.panel h2{font-size:14px;margin:0 0 10px;display:flex;align-items:center;justify-content:space-between;gap:8px}
.pill-row{display:flex;flex-wrap:wrap;gap:6px}
.pill{font-size:12px;padding:3px 9px;border:1px solid var(--line);border-radius:999px;background:var(--bg)}
.pill.warn{background:var(--flag-bg);color:var(--flag);border-color:var(--flag)}
.pill.high{color:var(--urgent);border-color:var(--urgent);font-weight:700}
.pill.ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok)}
.mini-list{margin:0;padding:0;list-style:none;display:grid;gap:7px;font-size:13px}
.mini-item{border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--bg);overflow-wrap:anywhere}
.mini-btn{font-size:12px;padding:3px 8px;margin-top:4px}
button,input,select{font:inherit;color:inherit}button,select,input[type=search]{
 border:1px solid var(--line);background:var(--card);border-radius:9px;padding:8px 11px}
button{cursor:pointer;white-space:nowrap}button:hover{border-color:var(--accent)}
:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
.toolbar{position:sticky;top:0;z-index:3;background:var(--bg);border-block:1px solid var(--line)}
.toolbar-inner{padding-block:12px;display:flex;align-items:end;gap:10px;flex-wrap:wrap}
.control{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted)}
.control input,.control select{font-size:14px;color:var(--ink);width:100%;min-width:125px}
.search{flex:1;min-width:200px}.check{padding:9px 0;white-space:nowrap;font-size:14px}
.check input{accent-color:var(--accent)}.status{color:var(--muted);font-size:13px;margin:0 0 18px}
.message{--sender:#566678;display:grid;grid-template-columns:40px minmax(0,1fr);gap:12px;margin:0 0 20px;
 content-visibility:auto;contain-intrinsic-size:auto 260px}
.avatar{width:38px;height:38px;display:grid;place-items:center;border-radius:12px;
 background:var(--sender);color:white;font-weight:800;font-size:11px;margin-top:4px}
.card{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--sender);
 border-radius:0 14px 14px 14px;padding:16px 20px;min-width:0}
.sender{font-weight:750;color:var(--sender)}.route{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.recipient{font-weight:600}.meta,.filename{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.meta{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:7px}.filename{margin-top:4px}
.badge{font-size:11px;padding:2px 8px;border:1px solid var(--line);border-radius:20px}
.attention{background:var(--flag-bg);color:var(--flag);border-color:var(--flag)}
.urgent{color:var(--urgent);border-color:var(--urgent);font-weight:750}
.legacy-badge{color:var(--muted);background:var(--bg)}
.needs .card{box-shadow:0 0 0 1px var(--flag)}.rejected .card{border-top:3px solid var(--urgent)}
.body{overflow-wrap:anywhere;margin-top:14px}.body>:first-child{margin-top:0}
.body h1{font-size:20px}.body h2{font-size:18px}.body h3{font-size:16px}
.body p{margin:10px 0}.body ul,.body ol{padding-left:24px}
a{color:var(--accent);text-underline-offset:3px}code{background:var(--bg);border-radius:4px;
 padding:2px 5px;font:.9em/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}
pre{background:var(--bg);padding:14px;border-radius:9px;overflow:auto;white-space:pre}
pre code{padding:0;background:none}blockquote{margin:12px 0;border-left:3px solid var(--line);
 padding-left:16px;color:var(--muted)}.table-wrap{overflow:auto;margin:14px 0}
table{border-collapse:collapse;min-width:50%;font-size:13px}th,td{padding:7px 10px;
 border:1px solid var(--line)}th{background:var(--bg)}.align-left{text-align:left}
.align-center{text-align:center}.align-right{text-align:right}
summary{cursor:pointer;margin-top:12px;list-style:none}summary::-webkit-details-marker{display:none}
.summary-title{font-size:17px;font-weight:700;line-height:1.5;overflow-wrap:anywhere}
.preview{display:block;white-space:pre-line;color:var(--muted);font-size:14px;line-height:1.6;
 max-height:5em;overflow:hidden;margin-top:8px;overflow-wrap:anywhere}
.toggle{display:block;color:var(--accent);font-size:12px;margin-top:8px}
.toggle::after{content:'展开全文 ↓'}details[open] .toggle::after{content:'收起 ↑'}
details[open] .preview{display:none}.empty{text-align:center;padding:50px;color:var(--muted)}
[hidden]{display:none!important}footer{text-align:center;color:var(--muted);font-size:12px;padding:20px}
@media(prefers-color-scheme:dark){:root{--bg:#141720;--card:#1d2230;--ink:#e6e9f3;
 --muted:#a3aec3;--line:#343d52;--accent:#b9a5ff;--flag:#f3be70;--flag-bg:#43331f;--urgent:#ff8295;--ok:#62d2b4;--ok-bg:#16382f}
 .sender{color:color-mix(in srgb,var(--sender) 60%,white)} }
"""


JS = r"""
(() => {
  'use strict';
  const messages = Array.from(document.querySelectorAll('.message'));
  const sender = document.getElementById('sender');
  const inbox = document.getElementById('inbox');
  const thread = document.getElementById('thread');
  const kind = document.getElementById('kind');
  const needsHuman = document.getElementById('needs-human');
  const search = document.getElementById('search');
  const status = document.getElementById('status');
  const empty = document.getElementById('empty');
  const index = messages.map(el => ({el, text: el.dataset.search.toLocaleLowerCase()}));

  function filter() {
    const terms = search.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    let visible = 0;
    index.forEach(({el, text}) => {
      const toVal = el.dataset.to;
      const fromVal = el.dataset.sender;
      const inboxMatch = !inbox.value || toVal === inbox.value || (toVal === 'all' && fromVal !== inbox.value && inbox.value !== 'all');
      const show = (!sender.value || fromVal === sender.value)
        && inboxMatch
        && (!thread.value || el.dataset.thread === thread.value || el.dataset.task === thread.value)
        && (!kind.value || el.dataset.kind === kind.value)
        && (!needsHuman.checked || el.dataset.human === 'yes')
        && terms.every(term => text.includes(term));
      el.hidden = !show;
      if (show) visible++;
    });
    status.textContent = `显示 ${visible} / ${messages.length} 条消息 · UTC`;
    empty.hidden = visible !== 0;
  }

  [sender, inbox, thread, kind, needsHuman].forEach(el => el.addEventListener('change', filter));
  search.addEventListener('input', filter);
  document.getElementById('reset').addEventListener('click', () => {
    sender.value = ''; inbox.value = ''; thread.value = ''; kind.value = ''; needsHuman.checked = false; search.value = ''; filter();
  });
  document.querySelectorAll('[data-filter-thread]').forEach(btn => {
    btn.addEventListener('click', () => {
      thread.value = btn.dataset.filterThread;
      filter();
      document.getElementById('timeline').scrollIntoView({behavior: 'smooth'});
    });
  });
  document.querySelectorAll('[data-filter-inbox]').forEach(btn => {
    btn.addEventListener('click', () => {
      inbox.value = btn.dataset.filterInbox;
      filter();
      document.getElementById('timeline').scrollIntoView({behavior: 'smooth'});
    });
  });
  function latest() {
    const last = messages.filter(el => !el.hidden).pop();
    if (last) last.scrollIntoView({block: 'end'});
  }
  document.getElementById('latest').addEventListener('click', latest);
  filter();
})();
"""


def _render_dashboard(result: dict) -> str:
    state = result["state"]
    lint = result["lint"]
    health = result["health"]
    counts = state["counts"]

    # Tasks by state summary
    by_state: Dict[str, int] = {s: 0 for s in board_lint.TASK_STATES}
    for t in state["tasks"].values():
        by_state[t["state"]] = by_state.get(t["state"], 0) + 1

    health_cls = "ok" if health["healthy"] and not health["live_dispatch_suppressed"] else "high"
    health_label = "HEALTHY" if health["healthy"] else "UNHEALTHY / STALE"
    if health["live_dispatch_suppressed"]:
        health_label += " (LIVE DISPATCH SUPPRESSED)"

    human_label = escape(board_lint.HUMAN_ROLE)
    parts = [
        '<section class="dashboard" aria-label="BoardHub 仪表盘">',
        '<div class="panel">',
        '<h2><span>系统状态与摘要</span><span class="pill {}">{}</span></h2>'.format(
            health_cls, escape(health_label)
        ),
        '<div class="pill-row">',
        '<span class="pill">snapshot: <code>{}</code></span>'.format(escape(state["snapshot_id"])),
        '<span class="pill">as_of: <code>{}</code></span>'.format(escape(state["as_of"])),
        '<span class="pill">v2 已采纳: {}</span>'.format(counts["v2_accepted"]),
        '<span class="pill">v2 已拒绝: {}</span>'.format(counts["v2_rejected"]),
        '<span class="pill">旧版历史: {}</span>'.format(counts["legacy_messages"]),
        '<span class="pill {}">待 {} 决定: {}</span>'.format(
            "warn" if counts["pending_open"] else "", human_label, counts["pending_open"]
        ),
        '<span class="pill {}">超时任务 (&gt;24h): {}</span>'.format(
            "warn" if health["stale_task_count"] else "", health["stale_task_count"]
        ),
        '<span class="pill {}">Lint: high={} / error={} / warn={}</span>'.format(
            "high" if lint["counts"]["high"] else ("warn" if lint["counts"]["error"] else ""),
            lint["counts"]["high"], lint["counts"]["error"], lint["counts"]["warning"],
        ),
        "</div>",
        '<div class="pill-row" style="margin-top:8px">',
    ]
    for st in board_lint.TASK_STATES:
        parts.append('<span class="pill">{}: {}</span>'.format(escape(st), by_state.get(st, 0)))
    parts.append("</div></div>")

    parts.append('<div class="dash-grid">')

    # Pending-human panel
    parts.append('<div class="panel"><h2><span>待 {} 处理</span><span class="pill">{}</span></h2>'.format(
        human_label, len(state["pending"])
    ))
    if not state["pending"]:
        parts.append('<p class="subtitle">无待处理人工决策。</p>')
    else:
        parts.append('<ul class="mini-list">')
        for item in state["pending"]:
            label = item.get("task_label") or "-"
            parts.append(
                '<li class="mini-item"><strong>{}</strong> · thread <code>{}</code><br>'
                'trigger: <code>{}</code> · label: {}<br>'
                '<button type="button" class="mini-btn" data-filter-thread="{}">查看线程</button></li>'.format(
                    escape(item["reason"]),
                    escape(item["thread_id"]),
                    escape(item["trigger_uuid"]),
                    escape(label),
                    escape(item["thread_id"]),
                )
            )
        parts.append("</ul>")
    parts.append("</div>")

    # Active & Recent Tasks panel
    parts.append('<div class="panel"><h2><span>任务状态</span><span class="pill">{}</span></h2>'.format(
        len(state["tasks"])
    ))
    if not state["tasks"]:
        parts.append('<p class="subtitle">暂无 v2 任务。</p>')
    else:
        parts.append('<ul class="mini-list">')
        for tid in sorted(state["tasks"]):
            t = state["tasks"][tid]
            badges = ['<span class="pill">{}</span>'.format(escape(t["state"]))]
            if t["stale"]:
                badges.append('<span class="pill warn">STALE &gt;24h</span>')
            if t["paused"]:
                badges.append('<span class="pill high">PAUSED:{}</span>'.format(escape(t["pause_reason"])))
            parts.append(
                '<li class="mini-item"><strong>{}</strong> (<code>{}</code>) {}<br>'
                'assignee: <code>{}</code> · reviewer: <code>{}</code> · events: {}/{}<br>'
                '<button type="button" class="mini-btn" data-filter-thread="{}">筛选时间线</button></li>'.format(
                    escape(t["label"] or tid[:8]),
                    escape(tid),
                    " ".join(badges),
                    escape(t["roles"]["assignee"]),
                    escape(t["roles"]["reviewer"]),
                    t["event_count"],
                    board_lint.LOOP_BREAKER_LIMIT,
                    escape(tid),
                )
            )
        parts.append("</ul>")
    parts.append("</div>")

    # Paused / Frozen threads & Inboxes panel
    paused_threads = [thr for thr in state["threads"].values() if thr["paused"] or thr["frozen"]]
    parts.append('<div class="panel"><h2><span>暂停/冲突线程 &amp; 收件箱</span><span class="pill">{}</span></h2>'.format(
        len(paused_threads)
    ))
    parts.append('<div class="pill-row" style="margin-bottom:8px">')
    for agent in board_state.get_inbox_recipients():
        cnt = len(state["inboxes"].get(agent, []))
        parts.append(
            '<button type="button" class="mini-btn" data-filter-inbox="{}">{}: {}</button>'.format(
                escape(agent), escape(agent), cnt
            )
        )
    parts.append("</div>")
    if not paused_threads:
        parts.append('<p class="subtitle">无暂停或分叉冻结线程。</p>')
    else:
        parts.append('<ul class="mini-list">')
        for thr in sorted(paused_threads, key=lambda x: x["thread_id"]):
            cids = ", ".join(thr["conflict_ids"]) if thr["conflict_ids"] else "-"
            parts.append(
                '<li class="mini-item"><code>{}</code> · {}<br>'
                'events: {} · conflicts: <code>{}</code><br>'
                '<button type="button" class="mini-btn" data-filter-thread="{}">筛选线程</button></li>'.format(
                    escape(thr["thread_id"]),
                    escape(thr["pause_reason"] or ",".join(thr["freeze_reasons"])),
                    thr["event_count"],
                    escape(cids),
                    escape(thr["thread_id"]),
                )
            )
        parts.append("</ul>")
    parts.append("</div>")

    parts.append("</div>")  # end dash-grid

    # Lint warnings / errors table
    if lint["diagnostics"]:
        parts.append('<div class="panel"><h2><span>Lint 诊断摘要</span><span class="pill">{}</span></h2>'.format(
            len(lint["diagnostics"])
        ))
        parts.append('<div class="table-wrap"><table><thead><tr>'
                     '<th>Severity</th><th>Code</th><th>File</th><th>Message ID</th><th>Thread ID</th><th>Detail</th>'
                     '</tr></thead><tbody>')
        for d in lint["diagnostics"][:100]:
            parts.append(
                "<tr><td>{}</td><td><code>{}</code></td><td><code>{}</code></td>"
                "<td><code>{}</code></td><td><code>{}</code></td><td>{}</td></tr>".format(
                    escape(d["severity"]),
                    escape(d["code"]),
                    escape(d["file"]),
                    escape(d["message_id"] or "-"),
                    escape(d["thread_id"] or "-"),
                    escape(d["message"]),
                )
            )
        parts.append("</tbody></table></div></div>")

    parts.append("</section>")
    return "\n".join(parts)


def _render_v2_card(rec: dict) -> str:
    sender, recipient = rec["from"], rec["to"]
    needs = any(rec.get(k) == "yes" for k in ("needs_user", "needs_grogan", "needs_human"))
    status = rec["status"]
    classes = "message v2"
    if needs:
        classes += " needs"
    if status != "accepted":
        classes += " rejected"

    meta_pairs = [
        f"uuid: {rec['uuid']}",
        f"thread: {rec['thread']}",
        f"prev: {rec['prev']}",
    ]
    if rec.get("re"):
        meta_pairs.append(f"re: {rec['re']}")
    if rec.get("task"):
        meta_pairs.append(f"task: {rec['task']}")
    if rec.get("state"):
        meta_pairs.append(f"state: {rec['state']}")
    if rec.get("verdict"):
        meta_pairs.append(f"verdict: {rec['verdict']}")
    if rec.get("delivers") is not None:
        meta_pairs.append(f"delivers: {len(rec['delivers'])} files")
    if rec.get("checklist") is not None:
        meta_pairs.append(f"checklist: {len(rec['checklist'])}/21")

    search_text = "\n".join(
        [rec["filename"], rec["body"], sender, recipient, rec["type"], rec["uuid"], rec["thread"]]
        + meta_pairs
    )
    dt = board_lint.parse_utc_rfc3339(rec["sent_at"])
    ts_str = dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S") if dt else rec["sent_at"]
    data_kind = "v2_accepted" if status == "accepted" else "v2_diag"

    parts = [
        '<article class="{}" style="--sender:{}" data-sender="{}" data-to="{}" '
        'data-thread="{}" data-task="{}" data-kind="{}" data-human="{}" data-search="{}">'.format(
            classes,
            sender_color(sender),
            escape(sender),
            escape(recipient),
            escape(rec["thread"]),
            escape(rec.get("task") or ""),
            data_kind,
            "yes" if needs else "no",
            escape(search_text),
        ),
        '<div class="avatar" aria-hidden="true">{}</div><div class="card">'.format(escape(sender[:2].upper())),
        '<div class="route"><span class="sender">{}</span><span>→</span>'
        '<span class="recipient">{}</span><span class="badge">v2: {}</span>'.format(
            escape(sender), escape(recipient), escape(rec["type"])
        ),
    ]
    if status == "accepted":
        parts.append('<span class="badge pill ok">accepted</span>')
    else:
        parts.append('<span class="badge urgent">{}: {}</span>'.format(
            escape(status), escape(rec.get("reason_code") or "")
        ))
    if needs:
        parts.append('<span class="badge attention">需要 {}</span>'.format(escape(board_lint.HUMAN_ROLE)))

    parts.append('</div><div class="meta"><time datetime="{}">{} {}</time>'.format(
        escape(rec["sent_at"]), escape(ts_str), DISPLAY_TZ_LABEL
    ))
    for item in meta_pairs:
        parts.append("<span>{}</span>".format(escape(item)))
    parts.append('</div><div class="filename">{}</div>'.format(escape(rec["filename"])))
    parts.append(_render_collapsible_body(rec["body"]))
    parts.append("</div></article>")
    return "\n".join(parts)


def _render_legacy_card(msg: dict) -> str:
    meta = msg["metadata"]
    body = msg["body"]
    sender = meta.get("from", "")
    recipient = meta.get("to", "")
    needs = any(
        board_lint.is_yes(meta.get(k, ""))
        for k in ("needs_user", "needs_grogan", "needs_human")
    )
    urgent = board_lint.is_yes(meta.get("urgent", ""))
    classes = "message legacy" + (" needs" if needs else "") + (" rejected" if urgent else "")

    search_text = "\n".join([msg["filename"], body] + [f"{k}: {v}" for k, v in meta.items()])
    stamp = datetime.fromtimestamp(msg["mtime_ns"] / 1e9, DISPLAY_TZ)
    parts = [
        '<article class="{}" style="--sender:{}" data-sender="{}" data-to="{}" '
        'data-thread="" data-task="{}" data-kind="legacy" data-human="{}" data-search="{}">'.format(
            classes,
            sender_color(sender),
            escape(sender),
            escape(recipient),
            escape(meta.get("task", "")),
            "yes" if needs else "no",
            escape(search_text),
        ),
        '<div class="avatar" aria-hidden="true">{}</div><div class="card">'.format(escape(sender[:2].upper())),
        '<div class="route"><span class="sender">{}</span><span>→</span>'
        '<span class="recipient">{}</span><span class="badge">{}</span>'
        '<span class="badge legacy-badge">legacy</span>'.format(
            escape(sender), escape(recipient), escape(meta.get("type") or "未标类型")
        ),
    ]
    if needs:
        parts.append('<span class="badge attention">需要 {}</span>'.format(escape(board_lint.HUMAN_ROLE)))
    if urgent:
        parts.append('<span class="badge urgent">紧急</span>')

    parts.append('</div><div class="meta"><time datetime="{}">{} {}</time>'.format(
        escape(stamp.isoformat()), escape(stamp.strftime("%Y-%m-%d %H:%M:%S")), DISPLAY_TZ_LABEL
    ))
    if meta.get("task"):
        parts.append("<span>task: {}</span>".format(escape(meta["task"])))
    if meta.get("re"):
        if msg.get("re_ambiguous"):
            parts.append("<span>re: {} (ambiguous numeric ref; unlinked)</span>".format(escape(meta["re"])))
        elif msg.get("re_resolved"):
            parts.append("<span>re: {} ({})</span>".format(escape(meta["re"]), escape(msg["re_resolved"])))
        else:
            parts.append("<span>re: {}</span>".format(escape(meta["re"])))
    if meta.get("cc"):
        parts.append("<span>cc: {}</span>".format(escape(meta["cc"])))
    parts.append('</div><div class="filename">{}</div>'.format(escape(msg["filename"])))
    parts.append(_render_collapsible_body(body))
    parts.append("</div></article>")
    return "\n".join(parts)


def _render_collapsible_body(body: str) -> str:
    rendered = '<div class="body">{}</div>'.format(render_markdown(body))
    nonempty = [line.strip() for line in body.splitlines() if line.strip()]
    if len(body) > 800 or len(body.splitlines()) > 16:
        title_index = next((i for i, line in enumerate(nonempty) if HEADING.match(line)), 0)
        title = HEADING.sub(lambda m: m[2], nonempty[title_index]) if nonempty else "无标题留言"
        preview = "\n".join(nonempty[title_index + 1:title_index + 4])[:280]
        return (
            '<details><summary><span class="summary-title">{}</span>'
            '<span class="preview">{}</span><span class="toggle"></span>'
            "</summary>{}</details>".format(escape(title), escape(preview), rendered)
        )
    return rendered


def render_page(result: dict) -> str:
    legacy_msgs = sorted(
        result["legacy_messages"],
        key=lambda m: (m["mtime_ns"], m["number"], m["filename"]),
    )
    v2_records = result["v2_records"]
    total_count = len(legacy_msgs) + len(v2_records)

    all_senders = sorted(
        ({r["from"] for r in v2_records} | {m["metadata"].get("from", "") for m in legacy_msgs}) - {""}
    )
    all_inboxes = sorted(
        set(board_state.get_inbox_recipients())
        | {r["to"] for r in v2_records}
        | {m["metadata"].get("to", "") for m in legacy_msgs}
        - {""}
    )
    all_threads = sorted(
        ({r["thread"] for r in v2_records} | {m["metadata"].get("task", "") for m in legacy_msgs}) - {""}
    )

    def _opts(values):
        return "".join('<option value="{0}">{0}</option>'.format(escape(v)) for v in values)

    script_hash = base64.b64encode(hashlib.sha256(JS.encode("utf-8")).digest()).decode("ascii")
    csp = (
        "default-src 'none'; "
        f"script-src 'sha256-{script_hash}'; "
        "style-src 'unsafe-inline'; "
        "connect-src 'none'; "
        "img-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )

    cards_html = "\n".join(
        [_render_legacy_card(m) for m in legacy_msgs] + [_render_v2_card(r) for r in v2_records]
    )

    human_label = escape(board_lint.HUMAN_ROLE)
    return (
        '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        '<meta name="referrer" content="no-referrer"><title>BoardHub v2 · 协作留言板</title>'
        f"<style>{CSS}</style></head>\n"
        '<body><header class="masthead"><div><div class="eyebrow">BOARDHUB V2</div>'
        '<h1>BoardHub 协作看板与留言板</h1>'
        f'<div class="subtitle">单解析器确定性重建 · 只读离线视图 · 审批仍须 {human_label} 在执行会话确认</div></div>'
        '<button id="latest" type="button">↓ 最新消息</button></header>\n'
        + _render_dashboard(result)
        + '\n<nav class="toolbar" aria-label="筛选留言"><div class="toolbar-inner">\n'
        '<label class="control search">全文搜索<input id="search" type="search" placeholder="搜索正文、UUID、线程或文件名…" autocomplete="off"></label>\n'
        f'<label class="control">发送方<select id="sender"><option value="">全部</option>{_opts(all_senders)}</select></label>\n'
        f'<label class="control">收件箱<select id="inbox"><option value="">全部</option>{_opts(all_inboxes)}</select></label>\n'
        f'<label class="control">线程 / Task<select id="thread"><option value="">全部</option>{_opts(all_threads)}</select></label>\n'
        '<label class="control">格式/状态<select id="kind"><option value="">全部</option>'
        '<option value="v2_accepted">v2 已采纳</option>'
        '<option value="v2_diag">v2 拒绝/冻结/延迟</option>'
        '<option value="legacy">旧版历史</option></select></label>\n'
        f'<label class="check"><input id="needs-human" type="checkbox"> 需要 {human_label}</label>\n'
        '<button id="reset" type="button">重置</button></div></nav>\n'
        f'<main id="timeline"><p id="status" class="status" role="status" aria-live="polite">共 {total_count} 条消息 · {DISPLAY_TZ_LABEL}</p>\n'
        '<noscript><p>JavaScript 未启用：可阅读全部内容，筛选不可用。</p></noscript>\n'
        '<div id="empty" class="empty" hidden>没有匹配的留言，请调整筛选条件。</div>'
        '<section aria-label="聊天记录">\n'
        + cards_html
        + "</section></main>"
        f"<footer>BoardHub v2 · snapshot {escape(result['snapshot_id'])} · 无外部资源</footer>"
        f"<script>{JS}</script></body></html>\n"
    )


def generate(
    board_directory: Path,
    output: Path,
    runtime_dir: Optional[Path] = None,
    as_of: Optional[datetime] = None,
) -> dict:
    now = as_of or datetime.now(timezone.utc)
    manifest = {}
    hk_ids = set()
    runtime_corrupt = False
    if runtime_dir is not None:
        rdir = Path(runtime_dir)
        m, m_corrupt = board_state.load_manifest(rdir / "manifest.json")
        if m_corrupt:
            runtime_corrupt = True
        elif m is not None:
            manifest = m
        hk_ids, _hk, hk_corrupt = board_state.load_housekeeping_receipts(rdir / "housekeeping.json")
        if hk_corrupt:
            runtime_corrupt = True

    snapshot = board_lint.scan_board(Path(board_directory))
    result = board_state.rebuild(
        snapshot,
        manifest,
        now,
        housekeeping_ids=hk_ids,
        runtime_corrupt=runtime_corrupt,
    )
    html_text = render_page(result)
    Path(output).write_text(html_text, encoding="utf-8")
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Render BoardHub v2 offline HTML view.")
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
        "--runtime-dir",
        type=Path,
        default=None,
        help="Optional private runtime directory to include manifest/housekeeping.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("board_chat_v2.html"),
        help="Output HTML path (default: ./board_chat_v2.html).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    started = time.perf_counter()
    args = parse_args(argv)
    try:
        board_dir, _runtime_dir, _human_role = board_lint.resolve_cli_settings(args)
    except (board_lint.SecurityError, board_lint.FormatError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    result = generate(board_dir, args.output, runtime_dir=args.runtime_dir)
    c = result["state"]["counts"]
    print(
        f"Generated {args.output} | v2={c['v2_accepted']} legacy={c['legacy_messages']} "
        f"tasks={c['tasks']} pending={c['pending_open']} | {time.perf_counter() - started:.3f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
