"use strict";

const $ = (id) => document.getElementById(id);
const initial = JSON.parse($("board-data").textContent);
const offline = Boolean(initial.offline);
const stateNames = {
  open: "待领取", claimed: "已领取", in_progress: "进行中", blocked: "受阻",
  in_review: "复核中", done: "已完成", rejected: "已拒绝"
};
const typeNames = {
  info: "消息", question: "问题", answer: "回复", task: "任务", state: "状态",
  delivery: "交付", result: "结果", review: "复核", checkin: "签到", checkout: "签退",
  decision: "决定", task_update: "任务更新", legacy: "历史"
};
const routeNames = {
  ok: "路由就绪", inbox_only: "仅收件箱", disabled: "已停用", stale: "签到已过期",
  awaiting_checkin: "等待签到", unavailable: "路由不可用", local: "本机"
};
const choices = {approve: "同意", reject: "拒绝", revise: "需要修改"};
const reasons = {
  thread_fork: "话题出现分叉", second_revise: "二次退回", loop_breaker_30: "达到消息上限"
};
const decisionNotice = "这里的决定会通知各 agent；涉及测试集、下载、服务器配置、删除的操作，仍需你在执行它的那个会话里亲口确认。";
// Display-only mapping from board roles to group members (apps). Board files and routing are unchanged.
const MEMBERS = [
  {id: "claude_code", name: "Claude Code", short: "CC", roles: ["claude_main", "claude_side", "claude"], primary: "claude_main"},
  {id: "antigravity", name: "Antigravity", short: "AG", roles: ["gemini"], primary: "gemini"},
  {id: "codex", name: "Codex", short: "CX", roles: ["codex", "gpt"], primary: "codex"},
  {id: "dsh", name: "DSH", short: "DS", roles: ["dsh"], primary: "dsh"},
  {id: "grogan", name: "Grogan", short: "G", roles: ["user", "grogan", "human"], primary: "user"}
];
const memberById = new Map(MEMBERS.map((member) => [member.id, member]));
const LEGACY_ROLES = new Set(["claude", "gpt"]);
const legacyLabels = {claude: "Claude Code（旧 claude）", gpt: "Codex（旧 gpt）"};
const mentionAliases = {
  antigravity: "gemini", main: "claude_main", side: "claude_side", claudecode: "claude_main", grogan: "user"
};
const EVENT_TYPES = new Set(["checkin", "checkout", "state"]);
const QUIET_TYPES = new Set(["info", "legacy"]);
const GROUP_WINDOW = 5 * 60 * 1000;
const roleNames = {coordinate: "统筹", review: "复核", implement: "实现", run: "运行", literature: "文献", decide: "决定"};
const confidenceNames = {explicit: "消息自带", registry: "签到或唤醒记录", inferred: "按身份推断"};
const effortNames = {minimal: "min", low: "low", medium: "med", high: "high", xhigh: "xhigh"};
const sessionTone = {live: "live", idle: "idle", stale: "stale"};
const sessionStatusNames = {live: "活跃", idle: "空闲", stale: "已过期"};
const SESSION_VISIBLE = 4;
const mainlineChips = {main: "左·main", side: "旁·side"};
const annotationIcons = {viewing: "📎", task: "⚙", peer: "↪", image: "🖼", command: "⌨", other: "·"};
const ACTIVITY_LINES = 6;
const routeTone = {
  ok: "live", inbox_only: "idle", disabled: "idle", stale: "warn",
  awaiting_checkin: "warn", unavailable: "warn", local: "self"
};
const absoluteTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
});
const dayFormat = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Tokyo", month: "long", day: "numeric", weekday: "short"
});
const relative = new Intl.RelativeTimeFormat("zh-CN", {numeric: "always"});
const composer = $("composer");
const timeline = $("timeline");
const search = $("search");
const commentDrafts = new Map();
let view = initial.offline || null;
let revision = "";
let mode = "mainline";
// Until the user opens 主线 themselves, fall back to 群聊概览 while the backend has no mainline.
let mainlineChosen = false;
// Transcript text lives only in `view`; nothing below writes it to storage, the seen-set or search.
let mainlineFilter = "";
const openActivity = new Set();
let selection = "";
let focused = "";
let busy = false;
let polling = null;
let loaded = false;
let recipientSignature = "";
let searchFrame = 0;
let pollTimer = 0;
let threadIndex = new Map();
let messageIndex = new Map();
let threadRows = new Map();
let memberIndex = new Map();
const expandedIds = new Set();
const historyOpen = new Set();

function readSetting(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch (_) { return fallback; }
}

function storeSetting(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); }
  catch (_) { /* Storage can be disabled for private or offline sessions. */ }
}

const seen = new Set(readSetting("boardhub-seen", []));
const collapsedMembers = new Set(readSetting("boardhub-collapsed", []));
let theme = readSetting("boardhub-theme", "");
if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function button(label, className, callback) {
  const node = el("button", className, label);
  node.type = "button";
  if (callback) node.addEventListener("click", callback);
  return node;
}

function humanName() {
  const name = view && view.display_name;
  return name && name !== "user" ? name : "Grogan";
}

// Legacy senders include free-form ids such as "codex-method" or "gemini (Antigravity)".
function roleInfo(actor) {
  const raw = String(actor || "").toLowerCase().trim();
  const base = raw.split(/[\s(（]/)[0];
  if (base.startsWith("claude")) {
    return {member: "claude_code", session: base.startsWith("claude_side") ? "side" : "main"};
  }
  if (base.startsWith("gemini") || base.startsWith("antigravity")) return {member: "antigravity", session: ""};
  if (base.startsWith("codex") || base.startsWith("gpt")) return {member: "codex", session: ""};
  if (base.startsWith("dsh")) return {member: "dsh", session: ""};
  if (["user", "grogan", "human"].includes(base) || (view && view.human_roles.includes(actor))) {
    return {member: "grogan", session: ""};
  }
  return {member: "", session: ""};
}

function memberMeta(id) {
  return memberIndex.get(id) || memberById.get(id) || null;
}

function memberName(id) {
  if (id === "grogan" && view && view.display_name && view.display_name !== "user") return view.display_name;
  const meta = memberMeta(id);
  return (meta && meta.name) || id;
}

// Members from the C01 contract, falling back to the built-in catalog when the view has none.
function memberList() {
  const listed = Array.isArray(view.members) ? view.members.filter((member) => member && member.id) : [];
  return listed.length ? listed : MEMBERS;
}

// Short model slug: "claude-opus-5-5" → "opus-5.5", "gemini-4-carbon" → "carbon", "gpt-6.2-astra" → "6.2-astra".
function modelLabel(model, effort) {
  if (!model && !effort) return "";
  let slug = String(model || "");
  if (/^claude-/.test(slug)) slug = slug.replace(/^claude-/, "").replace(/-(\d+)-(\d+)$/, "-$1.$2");
  else slug = slug.replace(/^gemini-\d+(\.\d+)?-/, "").replace(/^gpt-/, "");
  const level = effort && effort !== "none" ? (effortNames[effort] || effort) : "";
  return [slug, level].filter(Boolean).join(" · ");
}

// Sender attribution per C01; every field is optional and falls back to the role mapping.
function attribution(row) {
  const given = row.attribution && typeof row.attribution === "object" ? row.attribution : {};
  const fallback = roleInfo(row.from);
  const member = given.member || fallback.member;
  const sessionId = given.session_id ? String(given.session_id) : "";
  const label = given.session_label || (sessionId ? "#" + sessionId.slice(0, 8) : (member === fallback.member ? fallback.session : ""));
  return {
    member, label, sessionId, model: given.model || "", effort: given.effort || "",
    role: given.role || "", confidence: given.confidence || ""
  };
}

function speakerKey(row) {
  const a = attribution(row);
  return [row.from, a.member, a.sessionId || a.label, a.model, a.effort].join("|");
}

function roleTargetName(target) {
  if (!target || !target.member) return "";
  const slot = target.slot || (target.session_id ? "#" + String(target.session_id).slice(0, 8) : "");
  return memberName(target.member) + (slot ? " · " + slot : "");
}

function actorName(actor) {
  if (actor === "all") return "所有成员";
  const info = roleInfo(actor);
  if (!info.member) return actor || "未知";
  return memberName(info.member) + (info.session ? " · " + info.session : "");
}

function actorNames(actors) {
  return [...new Set(actors.filter((actor) => actor && actor !== "all").map(actorName))];
}

function membersOf(actors) {
  const ids = [];
  for (const actor of actors) {
    const id = roleInfo(actor).member;
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

function memberAvatar(id, label) {
  const known = memberById.get(id);
  const meta = memberMeta(id);
  const text = (meta && meta.short) || (meta && meta.name) || label || "?";
  const chip = el("span", "avatar", String(text).slice(0, 2).toUpperCase());
  chip.classList.add(known ? "m-" + id : "m-other");
  // Known members keep the tested palette; others may use the catalog colour.
  if (!known && meta && /^#[0-9a-f]{6}$/i.test(meta.color || "")) chip.style.background = meta.color;
  chip.setAttribute("aria-hidden", "true");
  return chip;
}

function avatar(actor) {
  return memberAvatar(roleInfo(actor).member, actor);
}

function cleanTitle(text) {
  return String(text || "").replace(/\*\*|__|`/g, "").replace(/\s+/g, " ").trim() || "未命名话题";
}

function plainText(text, limit = 180) {
  const flat = String(text || "")
    .replace(/```[\s\S]*?(```|$)/g, " ")
    .replace(/\[([^\]\n]+)\]\([^)\s]+\)/g, "$1")
    .replace(/^\s{0,3}(#{1,6}|>|[-+*]|\d+[.)])\s+/gm, "")
    .replace(/\*\*|__|`/g, "")
    .replace(/\s+/g, " ").trim();
  return flat.length > limit ? flat.slice(0, limit - 1) + "…" : flat;
}

function relativeLabel(at) {
  const seconds = (Date.parse(at) - Date.now()) / 1000;
  const steps = [[86400, "day"], [3600, "hour"], [60, "minute"]];
  for (const [length, unit] of steps) {
    if (Math.abs(seconds) >= length) return relative.format(Math.trunc(seconds / length), unit);
  }
  return "刚刚";
}

function timeNode(at, text = relativeLabel(at)) {
  const node = el("time", "", text);
  node.dateTime = at;
  node.title = absoluteTime.format(new Date(at)) + " JST";
  return node;
}

function updateTimes() {
  for (const node of document.querySelectorAll("time[data-relative]")) node.textContent = relativeLabel(node.dateTime);
}

function markSeen(messages) {
  if (offline) return;
  const size = seen.size;
  for (const row of messages) seen.add(row.id);
  if (size !== seen.size) storeSetting("boardhub-seen", [...seen].slice(-20000));
}

function safeLink(value) {
  if (!/^(https?:\/\/|mailto:)/i.test(value) || /[\x00-\x20\x7f\\]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (["http:", "https:", "mailto:"].includes(url.protocol)) return url.href;
  } catch (_) { /* Leave malformed URLs as text. */ }
  return null;
}

function inline(container, text) {
  // All unmatched fragments and Markdown contents enter the DOM as text nodes.
  const tokens = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|\[([^\]\n]+)\]\(([^\s()]+)\)/g;
  let end = 0;
  for (const match of text.matchAll(tokens)) {
    container.append(text.slice(end, match.index));
    const [, code, bold, boldAlt, emphasis, label, href] = match;
    if (code) container.append(el("code", "", code));
    else if (bold || boldAlt) container.append(el("strong", "", bold || boldAlt));
    else if (emphasis) container.append(el("em", "", emphasis));
    else {
      const url = safeLink(href);
      if (url) {
        const link = el("a", "", label);
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        container.append(link);
      } else container.append(match[0]);
    }
    end = match.index + match[0].length;
  }
  container.append(text.slice(end));
}

function markdown(text) {
  const content = el("div", "markdown");
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const listPattern = /^\s*([-+*]|\d+[.)])\s+(.*)$/;
  const fencePattern = /^\s{0,3}(`{3,}|~{3,})(.*)$/;
  const headingPattern = /^ {0,3}#{1,6}\s+(.+)$/;
  let paragraph = [];
  let list = null;
  function flush() {
    if (!paragraph.length) return;
    const block = el("p");
    inline(block, paragraph.join("\n"));
    content.append(block);
    paragraph = [];
  }
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const fence = line.match(fencePattern);
    const heading = line.match(headingPattern);
    const item = line.match(listPattern);
    if (!line.trim()) {
      flush();
      list = null;
    } else if (fence) {
      flush();
      list = null;
      const body = [];
      const close = new RegExp("^\\s{0,3}" + fence[1][0] + "{" + fence[1].length + ",}\\s*$");
      while (++i < lines.length && !close.test(lines[i])) body.push(lines[i]);
      const block = el("details");
      const language = fence[2].trim().slice(0, 24);
      block.append(el("summary", "expand-label", language ? "展开代码 · " + language : "展开代码"));
      const pre = el("pre");
      pre.append(el("code", "", body.join("\n")));
      block.append(pre);
      content.append(block);
    } else if (heading || line.startsWith(">")) {
      flush();
      list = null;
      const block = el(heading ? "h3" : "blockquote");
      inline(block, heading ? heading[1] : line.replace(/^>\s?/, ""));
      content.append(block);
    } else if (item) {
      flush();
      const tag = /^\d/.test(item[1]) ? "ol" : "ul";
      if (!list || list.localName !== tag) {
        list = el(tag);
        content.append(list);
      }
      const bullet = el("li");
      inline(bullet, item[2]);
      list.append(bullet);
    } else {
      list = null;
      paragraph.push(line);
    }
  }
  flush();
  return content;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options, credentials: "same-origin",
    headers: {"X-BoardHub-CSRF": initial.csrf, ...options.headers}
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "暂时无法完成请求。");
  return payload;
}

function showStatus(text, error = false) {
  const node = $("status");
  node.hidden = !text;
  node.textContent = text;
  node.classList.toggle("error", error);
}

function fileLink(row, label) {
  const link = el("a", "full-link", label);
  const address = "/api/file?path=" + encodeURIComponent(row.path);
  if (offline) {
    link.href = view.board_url + "/" + encodeURIComponent(row.path);
    link.title = row.path;
  } else {
    link.href = address;
    link.title = row.path;
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      try {
        const payload = await api(address);
        $("file-name").textContent = row.path;
        $("file-content").textContent = payload.body;
        $("file-dialog").showModal();
      } catch (error) {
        showStatus(error.message, true);
      }
    });
  }
  return link;
}

function focusMessage(row) {
  markSeen([row]);
  focused = row.id;
  for (const article of $("messages").querySelectorAll("[data-id]")) {
    article.classList.toggle("active", article.dataset.id === focused);
  }
  renderAgents();
  renderDetails();
  togglePanel("inspector", true);
}

function senderName(row) {
  const a = attribution(row);
  if (a.member === "grogan" && row.display_name) return row.display_name;
  return a.member ? memberName(a.member) : (row.from || "未知");
}

function senderLabel(row) {
  const a = attribution(row);
  return senderName(row) + (a.label ? " · " + a.label : "");
}

function typeLabel(type) {
  return Object.hasOwn(typeNames, type) ? typeNames[type] : type;
}

function messageBody(row) {
  const wrap = el("div", "message-body");
  const body = markdown(row.body);
  const long = row.more || row.body.length > 720 || row.body.split("\n").length > 16;
  if (!long) {
    wrap.append(body);
  } else {
    const clamp = el("div", "clamp");
    clamp.classList.toggle("open", expandedIds.has(row.id));
    clamp.append(body);
    const toggle = button("", "text-button more-toggle");
    const sync = () => {
      const open = expandedIds.has(row.id);
      clamp.classList.toggle("open", open);
      toggle.textContent = open ? "收起" : "展开全文";
      toggle.setAttribute("aria-expanded", String(open));
    };
    toggle.addEventListener("click", () => {
      if (expandedIds.has(row.id)) expandedIds.delete(row.id);
      else expandedIds.add(row.id);
      sync();
    });
    sync();
    const actions = el("div", "body-actions");
    actions.append(toggle);
    if (row.more) actions.append(fileLink(row, "查看完整原文"));
    wrap.append(clamp, actions);
  }
  if (row.related_thread && threadIndex.has(row.related_thread)) {
    wrap.append(button("查看原话题", "text-button", () => navigate("thread", row.related_thread)));
  }
  return wrap;
}

function messageCard(row, continued = false) {
  const article = el("article", "message");
  article.dataset.id = row.id;
  if (attribution(row).member === "grogan") article.classList.add("mine");
  if (row.needs) article.classList.add("needs");
  if (focused === row.id) article.classList.add("active");
  const gutter = el("div", "gutter");
  const content = el("div", "message-content");
  const timestamp = timeNode(row.at);
  timestamp.dataset.relative = "";
  if (continued) {
    article.classList.add("continued");
    const stamp = button("", "gutter-time text-button", () => focusMessage(row));
    stamp.append(timeNode(row.at, new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(new Date(row.at))));
    stamp.setAttribute("aria-label", "消息 #" + row.number);
    gutter.append(stamp);
    if (row.needs) content.append(el("span", "badge attention", "待决定"));
  } else {
    const a = attribution(row);
    gutter.append(memberAvatar(a.member, row.from));
    const meta = el("header", "message-meta");
    meta.append(el("strong", "sender", senderName(row)));
    if (a.label) {
      const session = el("span", "session-label" + (/^#/.test(a.label) ? " mono" : ""), a.label);
      if (a.sessionId) session.title = "会话 " + a.sessionId;
      meta.append(session);
    }
    const model = modelLabel(a.model, a.effort);
    if (model) {
      const pill = el("span", "model-pill", model);
      pill.title = [a.model, a.effort].filter(Boolean).join(" · ");
      meta.append(pill);
    }
    if (a.role) meta.append(el("span", "role-chip", roleNames[a.role] || a.role));
    meta.append(el("span", "destination", "→ " + actorName(row.to)));
    if (!QUIET_TYPES.has(row.type)) meta.append(el("span", "badge", typeLabel(row.type)));
    if (row.needs) meta.append(el("span", "badge attention", "待决定"));
    if (row.reason_code) meta.append(el("span", "badge", row.reason_code));
    meta.append(timestamp, button("#" + row.number, "text-button number", () => focusMessage(row)));
    content.append(meta);
  }
  content.append(messageBody(row));
  article.append(gutter, content);
  return article;
}

function eventRow(row) {
  const article = el("article", "event");
  article.dataset.id = row.id;
  if (focused === row.id) article.classList.add("active");
  const verb = {checkin: "签到了", checkout: "签退了", state: "更新了任务状态"}[row.type] || typeLabel(row.type);
  const text = el("p", "event-text");
  text.append(el("strong", "", senderLabel(row)), " " + verb);
  const note = plainText(row.body, 90);
  if (note && !/^check ?(in|out)\.?$/i.test(note)) text.append(el("span", "event-note", " · " + note));
  const timestamp = timeNode(row.at);
  timestamp.dataset.relative = "";
  article.append(memberAvatar(attribution(row).member, row.from), text, timestamp,
    button("#" + row.number, "text-button number", () => focusMessage(row)));
  return article;
}

function decisionNote() {
  const note = el("p", "decision-note");
  note.append(el("span", "", "◇"), " " + decisionNotice);
  note.firstChild.setAttribute("aria-hidden", "true");
  return note;
}

function decisionControls(item) {
  const controls = el("div", "decision-actions");
  controls.append(decisionNote());
  const label = el("label", "subtle", "评论（选填）");
  const comment = el("textarea");
  comment.rows = 2;
  comment.value = commentDrafts.get(item.trigger_uuid) || "";
  comment.addEventListener("input", () => commentDrafts.set(item.trigger_uuid, comment.value));
  label.append(comment);
  const actions = el("div", "decision-buttons");
  for (const [value, name] of Object.entries(choices)) {
    actions.append(button(name, value === "approve" ? "primary" : "secondary", async () => {
      const result = await post({
        action: "decide", thread: item.thread_id, item: item.trigger_uuid,
        decision: value, comment: comment.value
      });
      if (result) commentDrafts.delete(item.trigger_uuid);
    }));
  }
  controls.append(label, actions);
  return controls;
}

function decisionCard(item) {
  const card = el("section", "queue-item");
  const thread = threadIndex.get(item.thread_id);
  const row = messageIndex.get(item.trigger_uuid);
  card.append(el("h2", "", cleanTitle(item.task_label || (thread && thread.title) || "待处理话题")));
  if (row) card.append(messageCard(row));
  if (item.reason !== "needs_user") card.append(el("p", "subtle", reasons[item.reason] || item.reason));
  if (!offline && item.reason === "needs_user") card.append(decisionControls(item));
  card.append(button("打开话题", "text-button", () => navigate("thread", item.thread_id)));
  return card;
}

function topBar() {
  const health = view.health;
  const tracker = el("span");
  tracker.append(el("span", "health-dot " + health.watcher), ({
    ok: "Watcher 正常", stale: "Watcher 已过期", unknown: "Watcher 未报告"
  })[health.watcher]);
  const scan = el("span", "", "上次扫描 ");
  scan.append(health.scan ? timeNode(health.scan, absoluteTime.format(new Date(health.scan)) + " JST") : "—");
  $("top-status").replaceChildren(
    tracker, scan, el("span", "", health.routes_ok + " 条路由就绪"),
    el("span", "", "邮件 " + (health.mail === null ? "未知" : (health.mail ? "开启" : "关闭")))
  );
  if (!health.board) $("top-status").append(el("span", "", "看板暂不可用"));
  $("group-name").textContent = view.group && view.group.name ? view.group.name : "群聊";
  $("updated").replaceChildren(offline ? "离线快照 · 更新于 " : "更新于 ", timeNode(view.generated));
  $("updated").lastChild.dataset.relative = "";
}

function statusLine(agent) {
  const line = el("small", "presence");
  line.append(el("span", "dot " + (routeTone[agent.route] || "idle")), routeNames[agent.route] || agent.route);
  if (agent.wake) {
    line.append(" · 唤醒于 ", timeNode(agent.wake));
    line.lastChild.dataset.relative = "";
  }
  return line;
}

function unreadPill(count) {
  const pill = el("span", "number-pill", count);
  pill.title = count + " 条未读";
  return pill;
}

function sessionDot(status) {
  return el("span", "dot " + (sessionTone[status] || "idle"));
}

function sessionRow(member, session) {
  const id = session.id ? String(session.id) : "";
  const key = id ? "id:" + id : "label:" + member.id + ":" + (session.label || "");
  const row = button("", "session-row", () => navigate("session", key));
  row.setAttribute("aria-current", String(mode === "session" && selection === key));
  const label = el("span", "agent-text");
  const head = el("span", "session-head");
  const name = el("span", "session-name", session.label || (id ? "#" + id.slice(0, 8) : "会话"));
  if (!session.label) name.classList.add("mono");
  head.append(sessionDot(session.status), name);
  label.append(head);
  const line = el("small", "presence", sessionStatusNames[session.status] || "");
  if (session.last_seen && !Number.isNaN(Date.parse(session.last_seen))) {
    if (line.textContent) line.append(" · ");
    const seenAt = timeNode(session.last_seen);
    seenAt.dataset.relative = "";
    line.append(seenAt);
  }
  const model = modelLabel(session.model, session.effort);
  if (model) {
    if (line.childNodes.length) line.append(" · ");
    line.append(el("span", "model-text", model));
  }
  if (line.childNodes.length) label.append(line);
  row.title = [id && "会话 " + id, session.model, session.effort].filter(Boolean).join(" · ");
  row.append(label);
  return row;
}

function slotRow(role, agent) {
  const row = button("", "session-row", () => navigate("agent", role));
  row.setAttribute("aria-current", String(mode === "agent" && selection === role));
  const label = el("span", "agent-text");
  const head = el("span", "session-head");
  head.append(el("span", "session-name", roleInfo(role).session));
  label.append(head, statusLine(agent));
  if (agent.session) label.lastChild.title = agent.session;
  row.append(label);
  return row;
}

function renderAgents() {
  const nodes = [];
  const unread = new Map();
  for (const row of view.messages) {
    const fromMember = attribution(row).member;
    if ((row.to === "all" || roleInfo(row.to).member === "grogan") && !seen.has(row.id) && fromMember !== "grogan") {
      const key = fromMember || row.from;
      unread.set(key, (unread.get(key) || 0) + 1);
    }
  }
  const agents = new Map(view.agents.map((agent) => [agent.id, agent]));
  for (const member of memberList()) {
    const builtin = memberById.get(member.id);
    const roles = builtin ? builtin.roles.filter((role) => agents.has(role)) : [];
    const sessions = (Array.isArray(member.sessions) ? member.sessions : []).filter((session) => session && (session.id || session.label));
    if (!roles.length && !sessions.length && builtin && member.id !== "grogan") continue;
    const primary = (builtin && agents.get(builtin.primary)) || agents.get(roles[0]) || null;
    // Claude Code slots (main/side) stay visible from routes until discovery reports them as sessions.
    const labels = new Set(sessions.map((session) => session.label));
    const slots = member.id === "claude_code" ? ["claude_main", "claude_side"].filter((role) => agents.has(role) && !labels.has(roleInfo(role).session)) : [];
    const current = sessions.filter((session) => session.status !== "stale");
    const history = sessions.filter((session) => session.status === "stale");
    const visible = current.slice(0, SESSION_VISIBLE);
    const folded = [...current.slice(SESSION_VISIBLE), ...history];
    const childCount = visible.length + slots.length + folded.length;

    const block = el("div", "member");
    const head = el("div", "member-head");
    const row = button("", "agent-row", () => navigate("member", member.id));
    row.setAttribute("aria-current", String(mode === "member" && selection === member.id));
    const text = el("span", "agent-text");
    text.append(el("span", "member-name", memberName(member.id)));
    if (member.kind === "human" || member.id === "grogan") text.append(el("small", "presence", "你 · 做决定"));
    else {
      const live = sessions.filter((session) => session.status === "live").length;
      if (sessions.length || slots.length) {
        const summary = el("small", "presence");
        summary.append(sessionDot(live ? "live" : (primary && routeTone[primary.route] === "live" ? "live" : "idle")),
          (sessions.length + slots.length) + " 个会话" + (live ? "，" + live + " 个活跃" : ""));
        text.append(summary);
      } else if (primary) text.append(statusLine(primary));
    }
    row.append(memberAvatar(member.id, member.name), text);
    if (unread.has(member.id)) row.append(unreadPill(unread.get(member.id)));
    head.append(row);
    block.append(head);
    if (childCount) {
      const open = !collapsedMembers.has(member.id);
      const toggle = button(open ? "▾" : "▸", "disclosure", () => {
        if (collapsedMembers.has(member.id)) collapsedMembers.delete(member.id);
        else collapsedMembers.add(member.id);
        storeSetting("boardhub-collapsed", [...collapsedMembers]);
        renderAgents();
      });
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", (open ? "收起 " : "展开 ") + memberName(member.id) + " 的会话");
      head.prepend(toggle);
      block.classList.add("has-sessions");
      if (open) {
        const list = el("div", "session-list");
        for (const role of slots) list.append(slotRow(role, agents.get(role)));
        for (const session of visible) list.append(sessionRow(member, session));
        if (folded.length) {
          const showAll = historyOpen.has(member.id);
          if (showAll) for (const session of folded) list.append(sessionRow(member, session));
          const more = button(showAll ? "收起历史会话" : "+" + folded.length + " 个历史会话", "history-row", () => {
            if (historyOpen.has(member.id)) historyOpen.delete(member.id);
            else historyOpen.add(member.id);
            renderAgents();
          });
          more.setAttribute("aria-expanded", String(showAll));
          list.append(more);
        }
        block.append(list);
      }
    }
    nodes.push(block);
  }
  for (const agent of view.agents) {
    if (roleInfo(agent.id).member) continue;
    const row = button("", "agent-row", () => navigate("agent", agent.id));
    row.setAttribute("aria-current", String(mode === "agent" && selection === agent.id));
    const text = el("span", "agent-text");
    text.append(el("span", "member-name", agent.name || agent.id), statusLine(agent));
    row.append(avatar(agent.id), text);
    nodes.push(row);
  }
  $("agents").replaceChildren(...nodes);
}

function renderNavigation() {
  $("decision-count").textContent = view.pending.length;
  $("decision-count").classList.toggle("has", view.pending.length > 0);
  $("decisions-nav").setAttribute("aria-current", String(mode === "queue"));
  $("recent-nav").setAttribute("aria-current", String(mode === "recent" && !search.value.trim()));
  $("overview-nav").setAttribute("aria-current", String(mode === "overview" && !search.value.trim()));
  $("mainline-nav").setAttribute("aria-current", String(mode === "mainline" && !search.value.trim()));
  const threads = el("div", "thread-nav");
  for (const thread of view.threads) {
    if (mode === "task" && (!thread.task || (selection && selection !== thread.task.state))) continue;
    const row = button("", "thread-row", () => navigate("thread", thread.id));
    row.setAttribute("aria-current", String(mode === "thread" && selection === thread.id));
    const title = cleanTitle(thread.title);
    row.title = title;
    const summary = el("small", "", membersOf(thread.actors).map(memberName).join("、") + " · ");
    summary.append(timeNode(thread.at));
    summary.lastChild.dataset.relative = "";
    const head = el("span", "thread-row-head");
    if (thread.task) head.append(el("span", "state-dot s-" + thread.task.state));
    head.append(el("b", "", title));
    row.append(head, summary);
    threads.append(row);
  }
  if (!threads.childElementCount) threads.append(el("p", "subtle", "暂无话题"));
  $("threads").replaceChildren(threads);
  const taskCounts = {};
  for (const thread of view.threads) {
    if (thread.task) taskCounts[thread.task.state] = (taskCounts[thread.task.state] || 0) + 1;
  }
  const filters = [];
  for (const [key, label] of [["", "全部任务"], ...Object.entries(stateNames)]) {
    const count = key ? taskCounts[key] || 0 : Object.values(taskCounts).reduce((a, b) => a + b, 0);
    if (key && !count && !(mode === "task" && key === selection)) continue;
    const row = button("", "nav-row", () => navigate("task", key));
    row.setAttribute("aria-current", String(mode === "task" && key === selection));
    row.append(el("span", "nav-icon", ""), el("span", "", label));
    if (key) row.firstChild.append(el("span", "state-dot s-" + key));
    row.append(el("span", "number-pill", count));
    filters.push(row);
  }
  $("filters").replaceChildren(...filters);
}

function group(title) {
  const section = el("section", "detail-group");
  if (title) section.append(el("h3", "", title));
  return section;
}

function appendDetails(container, values) {
  const list = el("dl");
  for (const [label, value] of values) {
    if (value === undefined || value === null) continue;
    list.append(el("dt", "", label), el("dd", "", value));
  }
  container.append(list);
}

function makeRecipient(includeAll, selected, excluded = []) {
  const select = el("select");
  fillRecipients(select, includeAll, selected, excluded);
  return select;
}

function fillRecipients(select, includeAll, selected, excluded = []) {
  const ids = view.agents.map((agent) => agent.id).filter((id) => id !== "user" && !excluded.includes(id));
  const option = (id, label) => {
    const node = el("option", "", label);
    node.value = id;
    return node;
  };
  const nodes = [option("", "选择成员")];
  if (includeAll) nodes.push(option("all", "所有成员"));
  nodes.push(...ids.filter((id) => !LEGACY_ROLES.has(id)).map((id) => option(id, actorName(id))));
  const legacy = ids.filter((id) => LEGACY_ROLES.has(id));
  if (legacy.length) {
    const groupNode = el("optgroup");
    groupNode.label = "旧身份";
    groupNode.append(...legacy.map((id) => option(id, legacyLabels[id] || id)));
    nodes.push(groupNode);
  }
  select.replaceChildren(...nodes);
  select.value = ["", ...(includeAll ? ["all"] : []), ...ids].includes(selected) ? selected : "";
}

function rolesSummary() {
  const roles = view.roles && typeof view.roles === "object" ? Object.entries(view.roles) : [];
  const entries = roles.filter(([, targets]) => Array.isArray(targets) && targets.some((target) => target && target.member));
  if (!entries.length) return null;
  const section = group("群组职责");
  const list = el("dl", "roles");
  for (const [role, targets] of entries) {
    const names = targets.map(roleTargetName).filter(Boolean);
    const value = el("dd");
    value.append(el("span", "role-primary", names[0]));
    if (names.length > 1) value.append(el("span", "subtle", "、" + names.slice(1).join("、")));
    list.append(el("dt", "", roleNames[role] || role), value);
  }
  section.append(list);
  return section;
}

function renderDetails() {
  const details = $("details");
  const active = messageIndex.get(focused);
  const thread = threadIndex.get(mode === "thread" ? selection : active && active.thread);
  $("inspector-title").textContent = thread ? "话题详情" : "群聊信息";
  if (!thread) {
    const overview = group(view.group && view.group.name ? view.group.name : "群聊");
    overview.append(el("p", "subtle", "选择话题或一条消息以查看详情。"));
    details.replaceChildren(overview);
    const roles = rolesSummary();
    if (roles) details.append(roles);
    return;
  }
  const wakeChoice = $("wake-recipient")?.value;
  const assignmentChoice = $("assignment-recipient")?.value;
  const info = group(cleanTitle(thread.title));
  const task = thread.task;
  appendDetails(info, [
    ["状态", task ? stateNames[task.state] || task.state : (thread.locked ? "已暂停" : null)],
    ["负责人", task && actorName(task.roles.assignee)],
    ["复核人", task && task.roles.reviewer !== "none" ? actorName(task.roles.reviewer) : null],
    ["参与", actorNames(thread.actors).join("、")]
  ]);
  if (active) {
    const a = attribution(active);
    const who = senderLabel(active);
    appendDetails(info, [
      ["消息", "#" + active.number + " · " + typeLabel(active.type)],
      ["发送", who + (active.from && who.toLowerCase() !== active.from.toLowerCase() ? "（" + active.from + "）" : "")],
      ["模型", [a.model, a.effort].filter(Boolean).join(" · ") || null],
      ["职责", a.role ? roleNames[a.role] || a.role : null],
      ["依据", confidenceNames[a.confidence] || null],
      ["时间", absoluteTime.format(new Date(active.at)) + " JST"]
    ]);
    if (a.sessionId) {
      const id = el("code", "", a.sessionId);
      id.title = "会话 ID";
      info.append(id);
    }
  }
  if (mode !== "thread") info.append(button("打开话题", "text-button", () => navigate("thread", thread.id)));
  details.replaceChildren(info);
  if (!offline && thread.head) {
    const actions = group("操作");
    const participants = thread.actors.filter((id) => !view.human_roles.includes(id));
    const wake = makeRecipient(false, wakeChoice || (task ? task.roles.assignee : participants[0]));
    wake.id = "wake-recipient";
    wake.setAttribute("aria-label", "重新叫醒的成员");
    const controls = el("div", "action-row");
    controls.append(wake, button("重新叫醒", "secondary", () => post({action: "wake", thread: thread.id, to: wake.value})));
    actions.append(controls);
    if (task && !thread.locked) {
      const excluded = [task.roles.assignee, task.roles.reviewer];
      if (task.kind === "code" && task.roles.reviewer !== "codex") excluded.push("dsh");
      const assign = makeRecipient(false, assignmentChoice, excluded);
      assign.id = "assignment-recipient";
      assign.setAttribute("aria-label", "新负责人");
      const row = el("div", "action-row");
      row.append(assign, button("转交", "secondary", () => post({action: "reassign", thread: thread.id, to: assign.value})));
      const close = el("div", "action-row");
      close.append(button("关闭任务", "secondary", () => post({action: "close", thread: thread.id})));
      actions.append(row, close);
    }
    details.append(actions);
  }
  if (task) {
    const deliveries = group("交付与复核");
    if (!task.deliveries.length) deliveries.append(el("p", "subtle", "暂无交付"));
    for (const delivery of task.deliveries) {
      const status = delivery.verdict ? (choices[delivery.verdict] || delivery.verdict) : "等待复核";
      const entry = el("div", "detail-group");
      entry.append(el("p", "", status + " · "), timeNode(delivery.submitted_at));
      for (const [path, sha] of Object.entries(delivery.delivers)) entry.append(el("b", "", path), el("code", "", sha));
      deliveries.append(entry);
    }
    details.append(deliveries);
  }
  const logs = group("唤醒与邮件");
  const entries = view.logs[thread.id] || [];
  if (!entries.length) logs.append(el("p", "subtle", "暂无日志"));
  const logList = el("ul");
  for (const entry of entries.slice().reverse()) {
    const line = el("li", "log-entry");
    line.append(timeNode(entry.at, absoluteTime.format(new Date(entry.at)) + " JST"), " ",
      entry.route === "mail" ? "邮件" : actorName(entry.agent) + " · " + entry.route,
      " · " + entry.status + (entry.code ? " (" + entry.code + ")" : ""));
    logList.append(line);
  }
  logs.append(logList);
  details.append(logs);
  const files = group("消息文件");
  const fileList = el("ul");
  const rows = view.messages.filter((row) => row.thread === thread.id).slice().reverse();
  for (const row of rows) {
    const entry = el("li");
    entry.append(fileLink(row, row.path));
    if (active && active.id === row.id) entry.classList.add("subtle");
    fileList.append(entry);
  }
  files.append(fileList);
  details.append(files);
}

function updateComposer(resetTo = false) {
  const thread = mode === "thread" && threadIndex.get(selection);
  const to = composer.elements.to;
  const type = composer.elements.type;
  const isTask = type.value === "task";
  const label = $("composer-help");
  const blocked = Boolean((thread && (!thread.head || thread.locked)) && !isTask);
  composer.hidden = offline || mode === "queue" || mode === "mainline";
  $("mainline-hint").hidden = mode !== "mainline";
  $("new-message").hidden = offline;
  $("sending-as").textContent = "以 " + humanName() + " 发送";
  $("task-fields").hidden = !isTask;
  composer.elements.title.required = isTask;
  composer.elements.criteria.required = isTask;
  composer.elements.body.required = !isTask;
  const signature = view.agents.map((agent) => agent.id).join(",") + isTask;
  if (signature !== recipientSignature || resetTo) {
    let suggested = to.value;
    if (resetTo && mode === "agent" && selection !== "user") suggested = selection;
    if (resetTo && mode === "member" && memberById.has(selection) && selection !== "grogan") suggested = memberById.get(selection).primary;
    if (resetTo && thread) {
      const latest = view.messages.filter((row) => row.thread === thread.id).slice(-1)[0];
      suggested = (thread.task && thread.task.roles.assignee) ||
        (latest && (view.human_roles.includes(latest.from) ? latest.to : latest.from)) || "";
    }
    fillRecipients(to, !isTask, suggested);
    recipientSignature = signature;
  }
  composer.querySelector("button[type=submit]").disabled = busy || blocked;
  label.textContent = blocked ? "此话题已暂停或结束，可发送新消息开启新话题。" :
    (isTask ? "将创建一个新的任务话题" : (thread ? "回复到当前话题 · " : "") + "Enter 发送，Shift + Enter 换行");
}

function topicRow(thread) {
  const rows = threadRows.get(thread.id) || [];
  const card = el("article", "topic");
  card.addEventListener("click", (event) => {
    if (!event.target.closest("button, a")) navigate("thread", thread.id);
  });
  const head = el("div", "topic-head");
  const open = button(cleanTitle(thread.title), "topic-title", () => navigate("thread", thread.id));
  head.append(open);
  if (thread.task) {
    const chip = el("span", "state-chip");
    chip.append(el("span", "state-dot s-" + thread.task.state), stateNames[thread.task.state] || thread.task.state);
    head.append(chip);
  } else if (thread.locked) head.append(el("span", "state-chip", "已暂停"));
  if (rows.some((row) => row.needs)) head.append(el("span", "badge attention", "待决定"));
  const when = timeNode(thread.at);
  when.dataset.relative = "";
  when.className = "topic-time";
  head.append(when);
  card.append(head);

  const meta = el("div", "topic-meta");
  const stack = el("span", "avatar-stack");
  const members = membersOf(thread.actors);
  for (const id of members.slice(0, 4)) stack.append(memberAvatar(id));
  meta.append(stack, el("span", "", members.map(memberName).join("、")), el("span", "subtle", rows.length + " 条"));
  card.append(meta);

  const prose = rows.filter((row) => !EVENT_TYPES.has(row.type));
  const latest = prose.slice(-2);
  const heading = plainText(thread.title, 400);
  if (latest.length) {
    const list = el("div", "topic-lines");
    for (const row of latest) {
      const line = el("p", "topic-line");
      // Topic titles come from the root's first line (cut at 76 chars); skip repeating that line.
      const lines = row.body.split("\n");
      const first = lines.findIndex((line) => line.trim());
      const lead = first >= 0 ? plainText(lines[first], 400) : "";
      const rest = heading && lead && (lead.startsWith(heading) || heading.startsWith(lead));
      let text = plainText(rest ? lines.slice(first + 1).join("\n") : row.body, 400);
      text = text.length > 200 ? text.slice(0, 199) + "…" : text;
      if (!text) continue;
      line.append(el("b", "", senderLabel(row)), " " + text);
      list.append(line);
    }
    if (list.childElementCount) card.append(list);
  }
  const events = rows.length - prose.length;
  if (events) {
    const counts = {};
    for (const row of rows) if (EVENT_TYPES.has(row.type)) counts[row.type] = (counts[row.type] || 0) + 1;
    const parts = Object.entries(counts).map(([type, count]) => count + " 次" + ({checkin: "签到", checkout: "签退", state: "状态更新"}[type]));
    card.append(el("p", "topic-events", parts.join("，")));
  }
  return card;
}

function renderOverview(fragment) {
  const week = Date.now() - 7 * 86400000;
  const threads = view.threads.filter((thread) => Date.parse(thread.at) >= week);
  let previousDay = "";
  for (const thread of threads) {
    const day = dayFormat.format(new Date(thread.at));
    if (day !== previousDay) fragment.append(el("div", "day", day));
    previousDay = day;
    fragment.append(topicRow(thread));
  }
  return threads.length;
}

function renderRows(rows, fragment) {
  let previousDay = "";
  let previous = null;
  for (const row of rows) {
    const day = dayFormat.format(new Date(row.at));
    if (day !== previousDay) {
      fragment.append(el("div", "day", day));
      previous = null;
    }
    previousDay = day;
    if (EVENT_TYPES.has(row.type)) {
      fragment.append(eventRow(row));
      previous = null;
      continue;
    }
    const continued = Boolean(previous && speakerKey(previous) === speakerKey(row) && previous.to === row.to &&
      previous.thread === row.thread && previous.type === row.type && !row.needs &&
      Date.parse(row.at) - Date.parse(previous.at) <= GROUP_WINDOW);
    const card = messageCard(row, continued);
    if (mode !== "queue" && !offline && row.needs) {
      const pending = view.pending.find((item) => item.trigger_uuid === row.id && item.reason === "needs_user");
      if (pending) card.querySelector(".message-content").append(decisionControls(pending));
    }
    fragment.append(card);
    previous = row;
  }
}

function mainlineItems() {
  const mainline = view.mainline && typeof view.mainline === "object" ? view.mainline : null;
  if (!mainline || !Array.isArray(mainline.items)) return null;
  return mainline.items
    .filter((item) => item && item.id && !Number.isNaN(Date.parse(item.ts)))
    .sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts) || String(a.id).localeCompare(String(b.id)));
}

function sessionChip(label) {
  const chip = el("span", "session-chip s-" + (label === "main" || label === "side" ? label : "other"),
    mainlineChips[label] || label || "会话");
  return chip;
}

function mainlineBody(item) {
  const text = typeof item.text === "string" ? item.text : "";
  const body = markdown(text);
  const long = text.length > 900 || text.split("\n").length > 18;
  const key = "ml:" + item.id;
  if (!long) return body;
  const wrap = el("div");
  const clamp = el("div", "clamp");
  clamp.append(body);
  const toggle = button("", "text-button more-toggle");
  const sync = () => {
    const open = expandedIds.has(key);
    clamp.classList.toggle("open", open);
    toggle.textContent = open ? "收起" : "展开全文";
    toggle.setAttribute("aria-expanded", String(open));
  };
  toggle.addEventListener("click", () => {
    if (expandedIds.has(key)) expandedIds.delete(key);
    else expandedIds.add(key);
    sync();
  });
  sync();
  const actions = el("div", "body-actions");
  actions.append(toggle);
  wrap.append(clamp, actions);
  return wrap;
}

// App-injected context (C06): one grey line each above the turn's text. Only peer notes expand.
function annotationChips(item) {
  const list = Array.isArray(item.annotations) ? item.annotations.filter((note) => note && (note.label || note.detail)) : [];
  if (!list.length) return null;
  const wrap = el("div", "annotations");
  list.forEach((note, index) => {
    const kind = Object.hasOwn(annotationIcons, note.kind) ? note.kind : "other";
    const label = note.label || plainText(note.detail, 60);
    const detail = typeof note.detail === "string" ? note.detail.trim() : "";
    if (kind === "peer" && detail) {
      const key = "an:" + item.id + ":" + index;
      const open = expandedIds.has(key);
      const chip = button("", "annotation a-" + kind, () => {
        if (expandedIds.has(key)) expandedIds.delete(key);
        else expandedIds.add(key);
        renderTimeline(true);
      });
      chip.setAttribute("aria-expanded", String(open));
      chip.append(el("span", "annotation-icon", annotationIcons[kind]), el("span", "annotation-label", label),
        el("span", "ml-caret", open ? "▾" : "▸"));
      wrap.append(chip);
      if (open) {
        const body = el("div", "annotation-detail");
        body.append(markdown(detail));
        wrap.append(body);
      }
    } else {
      const chip = el("span", "annotation a-" + kind);
      chip.append(el("span", "annotation-icon", annotationIcons[kind]), el("span", "annotation-label", label));
      if (detail) chip.title = plainText(detail, 300);
      wrap.append(chip);
    }
  });
  return wrap;
}

function mainlineTurn(item) {
  const key = "ml:" + item.id;
  if (item.role === "event" || item.folded_default) {
    // Events, cross-session notes and compaction markers stay one line until opened.
    const article = el("article", "ml-event");
    const open = expandedIds.has(key);
    const first = plainText(item.text || "", 120) || (item.role === "event" ? "事件" : "消息");
    const head = button("", "ml-event-head", () => {
      if (expandedIds.has(key)) expandedIds.delete(key);
      else expandedIds.add(key);
      renderTimeline(true);
    });
    head.setAttribute("aria-expanded", String(open));
    head.append(el("span", "ml-caret", open ? "▾" : "▸"), sessionChip(item.session_label), el("span", "ml-event-text", first));
    const when = timeNode(item.ts);
    when.dataset.relative = "";
    head.append(when);
    article.append(head);
    if (open && item.text) {
      const body = el("div", "ml-event-body");
      body.append(markdown(item.text));
      article.append(body);
    }
    return article;
  }
  const mine = item.role === "user";
  const article = el("article", mine ? "ml-turn ml-user" : "ml-turn ml-assistant");
  const meta = el("header", "message-meta");
  const when = timeNode(item.ts);
  when.dataset.relative = "";
  const chips = annotationChips(item);
  const hasText = typeof item.text === "string" && item.text.trim() !== "";
  if (mine) {
    meta.append(el("strong", "sender", humanName()), sessionChip(item.session_label), when);
    const bubble = el("div", "ml-bubble");
    if (!hasText) bubble.classList.add("compact");
    bubble.append(meta);
    if (chips) bubble.append(chips);
    if (hasText) bubble.append(mainlineBody(item));
    article.append(bubble);
  } else {
    meta.append(el("strong", "sender", memberName("claude_code")), sessionChip(item.session_label), when);
    const content = el("div", "message-content");
    content.append(meta);
    if (chips) content.append(chips);
    if (hasText) content.append(mainlineBody(item));
    const gutter = el("div", "gutter");
    gutter.append(memberAvatar("claude_code"));
    article.append(gutter, content);
  }
  return article;
}

// Board activity between two turns: explicit refs plus anything posted in that time window, grouped by topic.
function activityCard(item, start, end) {
  const picked = new Map();
  for (const ref of Array.isArray(item.refs) ? item.refs : []) {
    const row = messageIndex.get(ref);
    if (row) picked.set(row.id, row);
  }
  for (const row of view.messages) {
    const at = Date.parse(row.at);
    if (at > start && at <= end && !EVENT_TYPES.has(row.type)) picked.set(row.id, row);
  }
  if (!picked.size) return null;
  const groups = new Map();
  for (const row of picked.values()) {
    if (!groups.has(row.thread)) groups.set(row.thread, []);
    groups.get(row.thread).push(row);
  }
  const ordered = [...groups.entries()].map(([thread, rows]) => {
    rows.sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
    return [thread, rows];
  }).sort((a, b) => Date.parse(a[1].at(-1).at) - Date.parse(b[1].at(-1).at));
  const key = item.id + "|" + start;
  const open = openActivity.has(key);
  const members = [];
  for (const row of picked.values()) {
    const id = attribution(row).member;
    if (id && id !== "grogan" && !members.includes(id)) members.push(id);
  }
  const card = el("section", "activity");
  const head = button("", "activity-head", () => {
    if (openActivity.has(key)) openActivity.delete(key);
    else openActivity.add(key);
    renderTimeline(true);
  });
  head.setAttribute("aria-expanded", String(open));
  const stack = el("span", "avatar-stack");
  for (const id of members.slice(0, 4)) stack.append(memberAvatar(id));
  head.append(el("span", "ml-caret", open ? "▾" : "▸"), stack,
    el("span", "activity-summary", "期间 agent 活动：" + ordered.length + " 个话题，" + picked.size + " 条消息"));
  card.append(head);
  if (open) {
    const list = el("div", "activity-list");
    for (const [threadId, rows] of ordered.slice(-ACTIVITY_LINES).reverse()) {
      const latest = rows.at(-1);
      const thread = threadIndex.get(threadId);
      const line = button("", "activity-line", () => navigate("thread", threadId));
      const who = senderName(latest) + " → " + actorName(latest.to);
      line.append(memberAvatar(attribution(latest).member, latest.from), el("span", "activity-who", who),
        el("span", "activity-title", cleanTitle(thread ? thread.title : latest.path)));
      if (thread && thread.task) {
        const chip = el("span", "state-chip");
        chip.append(el("span", "state-dot s-" + thread.task.state), stateNames[thread.task.state] || thread.task.state);
        line.append(chip);
      }
      if (rows.length > 1) line.append(el("span", "subtle", rows.length + " 条"));
      list.append(line);
    }
    if (ordered.length > ACTIVITY_LINES) list.append(el("p", "subtle activity-more", "还有 " + (ordered.length - ACTIVITY_LINES) + " 个更早的话题"));
    card.append(list);
  }
  return card;
}

function mainlineTools(items) {
  const tools = $("view-tools");
  if (mode !== "mainline" || search.value.trim() || !items) {
    tools.replaceChildren();
    return;
  }
  const labels = [...new Set(items.map((item) => item.session_label).filter(Boolean))];
  const group = el("div", "segmented");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "按会话筛选");
  for (const [value, name] of [["", "全部"], ...labels.map((label) => [label, mainlineChips[label] || label])]) {
    const option = button(name, "segment", () => {
      mainlineFilter = value;
      renderTimeline();
    });
    option.setAttribute("aria-pressed", String(mainlineFilter === value));
    group.append(option);
  }
  tools.replaceChildren(group);
}

function renderMainline(fragment) {
  const all = mainlineItems();
  if (!all) {
    const empty = el("div", "empty");
    empty.append(el("p", "", "主线暂不可用：服务还没有提供 Claude 对话记录。"),
      button("查看群聊概览", "text-button", () => navigate("overview")));
    fragment.append(empty);
    return {count: 0, labels: 0};
  }
  if (mainlineFilter && !all.some((item) => item.session_label === mainlineFilter)) mainlineFilter = "";
  const items = mainlineFilter ? all.filter((item) => item.session_label === mainlineFilter) : all;
  let previousDay = "";
  items.forEach((item, index) => {
    const day = dayFormat.format(new Date(item.ts));
    if (day !== previousDay) fragment.append(el("div", "day", day));
    previousDay = day;
    fragment.append(mainlineTurn(item));
    if (item.role === "assistant") {
      const next = items.slice(index + 1).find((other) => other.role === "user");
      const end = next ? Date.parse(next.ts) : Date.now();
      const card = activityCard(item, Date.parse(item.ts) - 1, end);
      if (card) fragment.append(card);
    }
  });
  if (!items.length) fragment.append(el("p", "empty", "还没有对话记录。"));
  return {count: items.length, labels: new Set(all.map((item) => item.session_label)).size};
}

function renderTimeline(keepScroll = false) {
  const prior = timeline.scrollTop;
  const atBottom = timeline.scrollHeight - timeline.clientHeight - prior < 75;
  const content = $("messages");
  const term = search.value.toLocaleLowerCase().trim();
  const thread = threadIndex.get(selection);
  let title = "全部消息";
  let caption = "";
  let rows = view.messages;
  const fragment = document.createDocumentFragment();
  content.classList.toggle("overview", !term && mode === "overview");
  content.classList.toggle("mainline", !term && mode === "mainline");
  mainlineTools(mainlineItems());

  if (term) {
    title = "搜索结果";
    rows = rows.filter((row) => {
      const a = attribution(row);
      return [row.from, senderLabel(row), row.display_name, row.to, actorName(row.to), a.model, a.sessionId,
        roleNames[a.role], row.number, "#" + row.number, row.body]
        .some((value) => String(value || "").toLocaleLowerCase().includes(term));
    });
  } else if (mode === "mainline") {
    title = "主线";
    const result = renderMainline(fragment);
    caption = "你与 Claude Code 的对话" + (result.labels > 1 ? "，main 与 side 按时间合并" : "") + " · " + result.count + " 条";
    rows = [];
  } else if (mode === "overview") {
    title = "群聊概览";
    const count = renderOverview(fragment);
    caption = "最近 7 天 · " + count + " 个话题，按最近活动排序";
    rows = [];
  } else if (mode === "queue") {
    title = "待你决定";
    caption = view.pending.length + " 项待处理";
    for (const item of [...view.pending].sort((a, b) => Date.parse(b.triggered_at) - Date.parse(a.triggered_at))) {
      fragment.append(decisionCard(item));
    }
    rows = [];
  } else if (mode === "thread") {
    title = thread ? cleanTitle(thread.title) : "话题";
    caption = thread ? (thread.task ? stateNames[thread.task.state] + " · " : "") + actorNames(thread.actors).join("、") : "";
    rows = threadRows.get(selection) || [];
    markSeen(rows);
  } else if (mode === "member") {
    title = memberName(selection);
    caption = "最近 7 天发送和收到的消息";
    rows = rows.filter((row) => row.recent && (attribution(row).member === selection || roleInfo(row.to).member === selection));
    markSeen(rows);
  } else if (mode === "session") {
    const [kind, ...rest] = selection.split(":");
    const id = kind === "id" ? rest.join(":") : "";
    const memberId = kind === "label" ? rest[0] : "";
    const label = kind === "label" ? rest.slice(1).join(":") : "";
    rows = rows.filter((row) => {
      const a = attribution(row);
      return id ? a.sessionId === id : (a.member === memberId && a.label === label && !a.sessionId);
    });
    const sample = rows[0] && attribution(rows[0]);
    const owner = sample ? sample.member : memberId;
    const session = (memberIndex.get(owner)?.sessions || []).find((item) => id ? String(item.id) === id : item.label === label);
    title = memberName(owner || "") + " · " + ((session && session.label) || label || "#" + id.slice(0, 8));
    caption = [session && modelLabel(session.model, session.effort), session && sessionStatusNames[session.status], "该会话发送的消息"]
      .filter(Boolean).join(" · ");
    markSeen(rows);
  } else if (mode === "agent") {
    title = actorName(selection);
    caption = "发送和收到的消息";
    rows = rows.filter((row) => row.from === selection || row.to === selection);
    markSeen(rows);
  } else if (mode === "task") {
    title = selection ? "任务 · " + stateNames[selection] : "全部任务";
    const tasks = view.threads.filter((item) => item.task && (!selection || item.task.state === selection));
    caption = tasks.length + " 项任务";
    const ids = new Set(tasks.map((item) => item.id));
    rows = rows.filter((row) => ids.has(row.thread));
  } else {
    caption = "最近 7 天";
    rows = rows.filter((row) => row.recent);
  }
  if (!caption) caption = rows.length + " 条消息";
  else if (rows.length) caption += " · " + rows.length + " 条消息";
  $("view-title").textContent = title;
  $("view-caption").textContent = caption;
  renderRows(rows, fragment);
  if (!fragment.childElementCount) {
    const empty = {queue: "没有待你决定的项目。", overview: "最近 7 天没有话题。发一条新消息开始讨论。"}[term ? "" : mode];
    fragment.append(el("p", "empty", empty || "没有符合条件的消息。"));
  }
  content.replaceChildren(fragment);
  if (mode === "queue" || mode === "overview" || term) timeline.scrollTop = keepScroll ? prior : 0;
  else if (mode === "mainline") timeline.scrollTop = keepScroll ? prior : timeline.scrollHeight;
  else timeline.scrollTop = keepScroll && !atBottom ? prior : timeline.scrollHeight;
}

function refreshIndexes() {
  threadIndex = new Map(view.threads.map((thread) => [thread.id, thread]));
  messageIndex = new Map(view.messages.map((message) => [message.id, message]));
  memberIndex = new Map((Array.isArray(view.members) ? view.members : [])
    .filter((member) => member && member.id).map((member) => [member.id, member]));
  threadRows = new Map();
  for (const row of view.messages) {
    if (!threadRows.has(row.thread)) threadRows.set(row.thread, []);
    threadRows.get(row.thread).push(row);
  }
}

function render(keepScroll = false, resetTo = false) {
  if (mode === "mainline" && !mainlineChosen && !mainlineItems()) mode = "overview";
  topBar();
  updateComposer(resetTo);
  renderTimeline(keepScroll);
  renderNavigation();
  renderAgents();
  renderDetails();
}

function togglePanel(id, open) {
  const pane = $(id);
  pane.classList.toggle("open", open);
  $(id === "sidebar" ? "nav-toggle" : "detail-toggle").setAttribute("aria-expanded", String(open));
}

function navigate(next, id = "") {
  if (!view) return;
  mode = next;
  selection = id;
  focused = "";
  search.value = "";
  togglePanel("sidebar", false);
  render(false, true);
}

async function post(payload) {
  if (busy) return null;
  busy = true;
  updateComposer();
  showStatus("正在发送…");
  try {
    const result = await api("/api/message", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)
    });
    if (polling) await polling;
    await poll();
    if (needsRender) {
      needsRender = false;
      render(true);
    }
    showStatus("已发送");
    return result;
  } catch (error) {
    showStatus(error.message, true);
    return null;
  } finally {
    busy = false;
    updateComposer();
  }
}

function poll() {
  if (polling) return polling;
  clearTimeout(pollTimer);
  polling = (async () => {
    try {
      const result = await api("/api/changes?since=" + encodeURIComponent(revision));
      if (result.changed) {
        if (result.reset) view = result.reset;
        else {
          const messages = new Map(view.messages.map((row) => [row.id, row]));
          for (const id of result.remove) messages.delete(id);
          for (const row of result.upsert) messages.set(row.id, row);
          const ordered = [...messages.values()].sort((a, b) =>
            Date.parse(a.at) - Date.parse(b.at) || a.at.localeCompare(b.at) ||
            a.number.localeCompare(b.number) || a.path.localeCompare(b.path));
          view = {...view, ...result.delta, messages: ordered};
        }
        revision = result.version;
        refreshIndexes();
        // Polling must not replace a comment or control while it has keyboard focus.
        if (!$("messages").contains(document.activeElement) && !$("details").contains(document.activeElement)) render(loaded);
        else needsRender = true;
        loaded = true;
      }
      if (pollError && !busy) showStatus("");
      pollError = false;
    } catch (error) {
      if (!busy) showStatus(error.message, true);
      pollError = true;
    }
    polling = null;
    pollTimer = setTimeout(poll, 3000);
  })();
  return polling;
}

let pollError = false;
let needsRender = false;
function deferredRender() {
  if (needsRender && !$("messages").contains(document.activeElement) && !$("details").contains(document.activeElement)) {
    needsRender = false;
    render(true);
  }
}
$("messages").addEventListener("focusout", () => setTimeout(deferredRender, 0));
$("details").addEventListener("focusout", () => setTimeout(deferredRender, 0));

$("theme").addEventListener("click", () => {
  const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  theme = dark ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  storeSetting("boardhub-theme", theme);
});
$("nav-toggle").addEventListener("click", () => togglePanel("sidebar", !$("sidebar").classList.contains("open")));
$("detail-toggle").addEventListener("click", () => togglePanel("inspector", !$("inspector").classList.contains("open")));
$("detail-close").addEventListener("click", () => togglePanel("inspector", false));
$("decisions-nav").addEventListener("click", () => navigate("queue"));
$("recent-nav").addEventListener("click", () => navigate("recent"));
$("overview-nav").addEventListener("click", () => navigate("overview"));
$("mainline-nav").addEventListener("click", () => {
  mainlineChosen = true;
  navigate("mainline");
});
$("new-message").addEventListener("click", () => {
  composer.elements.type.value = "info";
  navigate("overview");
  composer.elements.body.focus();
});
search.addEventListener("input", () => {
  if (!view) return;
  cancelAnimationFrame(searchFrame);
  searchFrame = requestAnimationFrame(() => render());
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    search.focus();
  }
  if (event.key === "Escape") {
    togglePanel("sidebar", false);
    togglePanel("inspector", false);
  }
});
composer.elements.type.addEventListener("change", () => updateComposer());
function mentionTarget(name) {
  const key = name.toLowerCase();
  return Object.hasOwn(mentionAliases, key) ? mentionAliases[key] : key;
}
composer.elements.body.addEventListener("input", () => {
  const match = composer.elements.body.value.match(/^\s*@([a-z_]+)(?=\s|$)/i);
  const target = match && mentionTarget(match[1]);
  if (target && Array.from(composer.elements.to.options).some((option) => option.value === target)) {
    composer.elements.to.value = target;
  }
});
composer.addEventListener("keydown", (event) => {
  if (event.target.name === "body" && event.key === "Enter" && !event.shiftKey
      && !event.isComposing && event.keyCode !== 229 && !composer.querySelector("[type=submit]").disabled) {
    event.preventDefault();
    composer.requestSubmit();
  }
});
composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(composer));
  const match = values.body.match(/^\s*@([a-z_]+)(?:\s+|$)/i);
  if (match && mentionTarget(match[1]) === values.to) values.body = values.body.slice(match[0].length);
  const result = await post({
    ...values, action: "send",
    thread: mode === "thread" && values.type !== "task" ? selection : ""
  });
  if (result) {
    composer.elements.body.value = "";
    composer.elements.title.value = "";
    composer.elements.criteria.value = "";
    composer.elements.type.value = "info";
    navigate("thread", result.thread);
  }
});
setInterval(updateTimes, 30000);
if (offline) {
  refreshIndexes();
  render();
} else poll();
