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
  thread_fork: "会话有冲突", second_revise: "二次退回", loop_breaker_30: "达到消息上限"
};
const initials = {claude_main: "CM", claude_side: "CS", claude: "C", codex: "CX", gemini: "G", dsh: "D", gpt: "GP", user: "我"};
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
let mode = "recent";
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

function readSetting(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch (_) { return fallback; }
}

function storeSetting(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); }
  catch (_) { /* Storage can be disabled for private or offline sessions. */ }
}

const seen = new Set(readSetting("boardhub-seen", []));
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

function actorName(actor) {
  return actor === "all" ? "所有成员" : (actor === "user" ? view.display_name : actor || "未知");
}

function avatar(actor) {
  const chip = el("span", "avatar", Object.hasOwn(initials, actor) ? initials[actor] : (actor || "?").slice(0, 2).toUpperCase());
  if (Object.hasOwn(initials, actor)) chip.classList.add(actor);
  else if (view.human_roles.includes(actor)) chip.classList.add("human");
  chip.setAttribute("aria-hidden", "true");
  return chip;
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
  for (const article of $("messages").querySelectorAll(".message")) {
    article.classList.toggle("active", article.dataset.id === focused);
  }
  renderAgents();
  renderDetails();
  togglePanel("inspector", true);
}

function messageCard(row, expanded) {
  const article = el("article", "message");
  article.dataset.id = row.id;
  if (view.human_roles.includes(row.from)) article.classList.add("mine");
  if (row.needs) article.classList.add("needs");
  if (focused === row.id) article.classList.add("active");
  const bubble = el("div", "bubble");
  const meta = el("header", "message-meta");
  const timestamp = timeNode(row.at);
  timestamp.dataset.relative = "";
  meta.append(
    el("strong", "", row.display_name || actorName(row.from)),
    el("span", "destination", "→ " + actorName(row.to)),
    el("span", "badge", Object.hasOwn(typeNames, row.type) ? typeNames[row.type] : row.type),
    timestamp,
    button("#" + row.number, "text-button", () => focusMessage(row))
  );
  if (row.needs) meta.append(el("span", "badge attention", "待决定"));
  if (row.reason_code) meta.append(el("span", "badge", row.reason_code));
  bubble.append(meta);
  const body = markdown(row.body);
  if (row.more || row.body.length > 720 || row.body.split("\n").length > 16) {
    const details = el("details");
    details.dataset.expand = row.id;
    details.open = expanded.has(row.id);
    details.append(el("summary", "expand-label", "展开"), body);
    bubble.append(details);
  } else bubble.append(body);
  if (row.more) bubble.append(fileLink(row, "查看完整原文"));
  if (row.related_thread && threadIndex.has(row.related_thread)) {
    bubble.append(button("查看原会话", "text-button", () => navigate("thread", row.related_thread)));
  }
  article.append(avatar(row.from), bubble);
  return article;
}

function decisionControls(item) {
  const controls = el("div", "decision-actions");
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

function decisionCard(item, expanded) {
  const card = el("section", "queue-item");
  const thread = threadIndex.get(item.thread_id);
  const row = messageIndex.get(item.trigger_uuid);
  card.append(el("h2", "", item.task_label || (thread && thread.title) || "待处理会话"));
  if (row) card.append(messageCard(row, expanded));
  if (item.reason !== "needs_user") card.append(el("p", "subtle", reasons[item.reason] || item.reason));
  if (!offline && item.reason === "needs_user") card.append(decisionControls(item));
  card.append(button("打开会话", "text-button", () => navigate("thread", item.thread_id)));
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
  $("updated").replaceChildren(offline ? "离线快照 · 更新于 " : "更新于 ", timeNode(view.generated));
  $("updated").lastChild.dataset.relative = "";
}

function renderAgents() {
  const nodes = [];
  const unread = new Map();
  for (const row of view.messages) {
    if ((row.to === "all" || view.human_roles.includes(row.to)) && !seen.has(row.id) && !view.human_roles.includes(row.from)) {
      unread.set(row.from, (unread.get(row.from) || 0) + 1);
    }
  }
  for (const agent of view.agents) {
    const row = button("", "agent-row", () => navigate("agent", agent.id));
    row.setAttribute("aria-current", String(mode === "agent" && selection === agent.id));
    const text = el("span", "agent-text");
    text.append(el("span", "", agent.name || agent.id));
    const hint = el("small", "", routeNames[agent.route] || agent.route);
    if (agent.wake) {
      hint.append(" · 唤醒于 ", timeNode(agent.wake));
      hint.lastChild.dataset.relative = "";
    }
    text.append(hint);
    if (agent.session) {
      const session = el("small", "session", "会话 " + agent.session);
      session.title = agent.session;
      text.append(session);
    }
    row.append(avatar(agent.id), text);
    if (unread.has(agent.id)) {
      const count = el("span", "number-pill", unread.get(agent.id));
      count.title = "未读";
      row.append(count);
    }
    nodes.push(row);
  }
  $("agents").replaceChildren(...nodes);
}

function renderNavigation() {
  $("decision-count").textContent = view.pending.length;
  $("decisions-nav").setAttribute("aria-current", String(mode === "queue"));
  $("recent-nav").setAttribute("aria-current", String(mode === "recent" && !search.value.trim()));
  const threads = el("div", "thread-nav");
  for (const thread of view.threads) {
    if (mode === "task" && (!thread.task || (selection && selection !== thread.task.state))) continue;
    const row = button("", "thread-row", () => navigate("thread", thread.id));
    row.setAttribute("aria-current", String(mode === "thread" && selection === thread.id));
    row.title = thread.title;
    const summary = el("small", "", thread.actors.map(actorName).join("、") + " · ");
    summary.append(timeNode(thread.at));
    summary.lastChild.dataset.relative = "";
    row.append(el("b", "", thread.title), summary);
    threads.append(row);
  }
  if (!threads.childElementCount) threads.append(el("p", "subtle", "暂无会话"));
  $("threads").replaceChildren(threads);
  const taskCounts = {};
  for (const thread of view.threads) {
    if (thread.task) taskCounts[thread.task.state] = (taskCounts[thread.task.state] || 0) + 1;
  }
  const filters = [];
  for (const [key, label] of [["", "全部任务"], ...Object.entries(stateNames)]) {
    const row = button(label, "nav-row", () => navigate("task", key));
    row.setAttribute("aria-current", String(mode === "task" && key === selection));
    const count = key ? taskCounts[key] || 0 : Object.values(taskCounts).reduce((a, b) => a + b, 0);
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
  const agents = ["", ...(includeAll ? ["all"] : []), ...view.agents
    .map((agent) => agent.id).filter((id) => id !== "user" && !excluded.includes(id))];
  select.replaceChildren(...agents.map((id) => {
    const option = el("option", "", id ? actorName(id) : "选择成员");
    option.value = id;
    return option;
  }));
  select.value = agents.includes(selected) ? selected : "";
}

function renderDetails() {
  const details = $("details");
  const active = messageIndex.get(focused);
  const thread = threadIndex.get(mode === "thread" ? selection : active && active.thread);
  if (!thread) {
    details.replaceChildren(el("p", "subtle", "选择会话或一条消息以查看详情。"));
    return;
  }
  const wakeChoice = $("wake-recipient")?.value;
  const assignmentChoice = $("assignment-recipient")?.value;
  const info = group(thread.title);
  const task = thread.task;
  appendDetails(info, [
    ["状态", task ? stateNames[task.state] || task.state : (thread.locked ? "已暂停" : null)],
    ["负责人", task && actorName(task.roles.assignee)],
    ["复核人", task && task.roles.reviewer !== "none" ? actorName(task.roles.reviewer) : null],
    ["成员", thread.actors.map(actorName).join("、")]
  ]);
  if (mode !== "thread") info.append(button("打开会话", "text-button", () => navigate("thread", thread.id)));
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
  composer.hidden = offline || mode === "queue";
  $("new-message").hidden = offline;
  $("sending-as").textContent = "以 " + view.display_name + " 发送";
  $("task-fields").hidden = !isTask;
  composer.elements.title.required = isTask;
  composer.elements.criteria.required = isTask;
  composer.elements.body.required = !isTask;
  const signature = view.agents.map((agent) => agent.id).join(",") + isTask;
  if (signature !== recipientSignature || resetTo) {
    let suggested = to.value;
    if (resetTo && mode === "agent" && selection !== "user") suggested = selection;
    if (resetTo && thread) {
      const latest = view.messages.filter((row) => row.thread === thread.id).slice(-1)[0];
      suggested = (thread.task && thread.task.roles.assignee) ||
        (latest && (view.human_roles.includes(latest.from) ? latest.to : latest.from)) || "";
    }
    fillRecipients(to, !isTask, suggested);
    recipientSignature = signature;
  }
  composer.querySelector("button[type=submit]").disabled = busy || blocked;
  label.textContent = blocked ? "此会话无法继续回复，可发送新消息。" :
    (isTask ? "创建独立任务会话" : "Enter 发送 · Shift + Enter 换行");
}

function renderTimeline(keepScroll = false) {
  const prior = timeline.scrollTop;
  const atBottom = timeline.scrollHeight - timeline.clientHeight - prior < 75;
  const content = $("messages");
  const expanded = new Set(Array.from(content.querySelectorAll("details[open][data-expand]"), (node) => node.dataset.expand));
  const term = search.value.toLocaleLowerCase().trim();
  const thread = threadIndex.get(selection);
  let title = "最近 7 天全部消息";
  let caption = "";
  let rows = view.messages;
  const fragment = document.createDocumentFragment();

  if (term) {
    title = "搜索结果";
    rows = rows.filter((row) => [row.from, row.display_name, row.to, row.number, "#" + row.number, row.body]
      .some((value) => String(value || "").toLocaleLowerCase().includes(term)));
  } else if (mode === "queue") {
    title = "待你决定";
    caption = view.pending.length + " 项待处理";
    for (const item of [...view.pending].sort((a, b) => Date.parse(b.triggered_at) - Date.parse(a.triggered_at))) {
      fragment.append(decisionCard(item, expanded));
    }
    rows = [];
  } else if (mode === "thread") {
    title = thread ? thread.title : "会话";
    caption = thread ? (thread.task ? stateNames[thread.task.state] + " · " : "") + thread.actors.map(actorName).join("、") : "";
    rows = rows.filter((row) => row.thread === selection);
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
    rows = rows.filter((row) => row.recent);
  }
  if (!caption) caption = rows.length + " 条消息";
  $("view-title").textContent = title;
  $("view-caption").textContent = caption;
  let previousDay = "";
  for (const row of rows) {
    const day = dayFormat.format(new Date(row.at));
    if (day !== previousDay) fragment.append(el("div", "day", day));
    previousDay = day;
    const card = messageCard(row, expanded);
    if (mode !== "queue" && !offline && row.needs) {
      const pending = view.pending.find((item) => item.trigger_uuid === row.id && item.reason === "needs_user");
      if (pending) card.querySelector(".bubble").append(decisionControls(pending));
    }
    fragment.append(card);
  }
  if (!fragment.childElementCount) {
    fragment.append(el("p", "empty", mode === "queue" && !term ? "没有待你决定的项目。" : "没有符合条件的消息。"));
  }
  content.replaceChildren(fragment);
  if (mode === "queue" || term) timeline.scrollTop = keepScroll ? prior : 0;
  else timeline.scrollTop = keepScroll && !atBottom ? prior : timeline.scrollHeight;
}

function refreshIndexes() {
  threadIndex = new Map(view.threads.map((thread) => [thread.id, thread]));
  messageIndex = new Map(view.messages.map((message) => [message.id, message]));
}

function render(keepScroll = false, resetTo = false) {
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
$("new-message").addEventListener("click", () => {
  composer.elements.type.value = "info";
  navigate("recent");
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
composer.elements.body.addEventListener("input", () => {
  const match = composer.elements.body.value.match(/^\s*@([a-z_]+)(?=\s|$)/i);
  if (match && Array.from(composer.elements.to.options).some((option) => option.value === match[1].toLowerCase())) {
    composer.elements.to.value = match[1].toLowerCase();
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
  if (match && match[1].toLowerCase() === values.to) values.body = values.body.slice(match[0].length);
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
