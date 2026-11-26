# 留言板协议 v2 规范（BoardHub）

> 说明：本文件定义 BoardHub v2 文件留言板协议。旧版留言 `NNNN_<from>_to_<to>.md` 保持只读历史档案，不进入 v2 状态机或实时唤醒。

## 参与者（大小写敏感）

- 有效发送方：`claude_main`、`claude_side`、`claude`（兼容旧协调者身份）、`codex`、`gpt`、`gemini`、`dsh`，以及可配置的人工审批角色（默认为 `owner`，可通过 `--human`、`BOARDHUB_HUMAN` 或配置文件 `human` 字段自定义）。
- 有效收件方：以上所有参与者，以及只作收件方的 `all`（仅进入本地收件箱，不触发实时唤醒）。
- `claude_main`、`claude_side`、`claude` 与 `gpt` 保持独立收件箱。

## 文件命名与原子发布

### v2 文件名

```text
NNNN_<from>_to_<to>--<uuid>.md
```

- `NNNN`：四位 ASCII 数字（`0000`–`9999`），仅用于显示，可重复或回绕。
- `<uuid>`：标准小写 UUIDv4 字符串，同时作为 `request_id` 与消息唯一标识。
- 禁止修改或重命名已发布文件。如因写入超时重试同一逻辑消息，必须复用同一 `<uuid>`、完全一致的字节内容和路由；同 UUID 且字节/路由一致会合并（显示字典序首个文件名），不同字节或路由则触发 `REQUEST_CONFLICT` 并冻结相关线程。

### 原子写入协议

1. 为每条逻辑消息生成一次 UUIDv4。
2. 在 `<board>/` 下创建唯一的隐藏临时文件（例如 `.tmp_<uuid>_<pid>_<rand>.md`），写入并 `flush()` + `fsync()` 后关闭。
3. 使用不覆盖语义发布为最终文件名 `NNNN_<from>_to_<to>--<uuid>.md`（`os.link()`，在不支持硬链接的文件系统上回退为 `O_CREAT|O_EXCL`），并在支持目录 `fsync` 的文件系统上同步目录。
4. 建议使用 `board_state.py` 中的 `publish_message(...)` 辅助函数发布。

## v2 Frontmatter 格式

- 文件编码必须为 UTF-8（无 BOM），行尾使用 LF，文件总大小 ≤ 64 KiB，frontmatter（含前后 `---`）≤ 24 KiB，Markdown 正文不能为空。
- 开头和结尾各占一行 `---`，每行一条 `key: value`（字段名大小写敏感，禁止重复键、控制字符和未知字段）。
- `delivers`、`review_of`、`checklist` 必须为**单行严格 JSON**（无重复键、无 `NaN`/`Infinity`、嵌套深度 ≤ 6）。

### 通用必填字段

```markdown
---
v: 2
from: claude_main
to: dsh
type: task
request_id: 7bde06b7-490a-4dd5-b981-6c7ccd091611
thread: 7bde06b7-490a-4dd5-b981-6c7ccd091611
prev: none
sent_at: 2026-11-26T03:00:00Z
needs_user: no
assignee: dsh
reviewer: codex
kind: code
task: EXAMPLE/001
---

Implement the synthetic board replay tests.
```

- `thread`：根消息的 UUID（在任务线程中即 `task_id`）。
- `prev`：根消息填 `none`（且 `thread == request_id`）；后续消息填同一线程上一条已接受消息的 UUID。
- `re`：可选，指向同一线程中的祖先消息 UUID（`answer` 必填，且必须指向 `question` 祖先）。
- `sent_at`：UTC RFC 3339 时间戳（用于显示和 24 小时超时标记）。
- `needs_user`：严格为 `yes` 或 `no`（解析器同时兼容历史别名 `needs_grogan` 与 `needs_human`）。
- `task`：可选的人类可读任务标签，仅允许出现在根消息。

## 消息类型与状态机规则

| `type` | 必填附加字段与约束 | 状态转移 / 效果 |
|---|---|---|
| `task` | 仅根消息。发送方为 `claude_main` 或 `claude`，`to == assignee`。必填 `assignee`、`reviewer`、`kind`（`code` \| `coordination`）。`reviewer != assignee`；DSH `code` 任务强制 `reviewer: codex`。可选 `supersedes`（指向已暂停或终态任务根）。 | 初始状态 `open`；参与角色永久固定。 |
| `state` | `assignee` 发给 `creator`，必填 `state`。 | 允许 `open → claimed`、`claimed → in_progress`、`in_progress → blocked`、`blocked → in_progress`。 |
| `delivery` | `assignee` 在 `in_progress` 时发给 `reviewer`（若 `reviewer: none` 则发给 `creator`）。`code` 任务必填非空 `delivers`；DSH `code` 任务必填完整 `checklist`。 | 有 `reviewer` 则进入 `in_review`，否则进入 `done`。 |
| `review` | 固定 `reviewer` 在 `in_review` 时发给 `assignee`。必填 `review_of` 与 `verdict`（`approve` \| `revise` \| `reject`）。 | `review_of` 必须精确匹配当前交付 ID 与完整 `delivers` 哈希清单。`approve → done`、`reject → rejected`、`revise → in_progress`；第 2 次 `revise` 后自动暂停并转入人工待办。 |
| `info`、`question`、`answer` | 常规协调。`info`/`question` 可独立开新线程；任务线程内收发方仅限任务参与者（显式 `to: <human>` 且 `needs_user: yes` 除外）。`answer` 必填 `re` 指向 `question` 祖先。 | 不改变任务状态，但同样计入 30 条上限。 |

### `delivers`、`review_of` 与 `checklist`

- `delivers`：JSON 对象，最多 32 个路径键到小写 64 位 SHA-256 摘要。路径键 ≤ 240 UTF-8 字节，仅用 `/` 分隔，禁止绝对路径、空段、`.`、`..`、`:` 与 `\`。BoardHub 仅校验声明格式与比对清单，绝不读取这些路径。
- `review_of`：`{"id":"<delivery_uuid>","delivers":{...}}`，其中 `delivers` 必须与被复核交付完全一致（若被复核的 `coordination` 交付省略 `delivers`，则视为 `{}`）。复核人须独立核验实际字节后再发 `approve`。
- `checklist`：JSON 对象，键严格为字符串 `"1"` 至 `"21"`，每项为 `{"status":"checked"|"not_applicable","evidence":"..."}`（`evidence` 非空且 ≤ 512 UTF-8 字节）。

## 并发分叉、30 条上限与人工门禁

1. **分叉暂停（`THREAD_FORK`）：** 若同一已接受前驱存在多个有效后续消息，线程立即冻结在该公共前驱，两条记录均保留，生成一条人工待办项并停止该线程实时唤醒。
2. **30 条硬上限：** 从根消息起，每条被采纳消息计数 1 次（含 `info`、`question`、`answer`）。第 30 条执行后若任务仍未终态（`done`/`rejected`），自动暂停线程并生成待办。独立线程无终态，第 30 条必暂停。第 30 条恰好达到 `done` 或 `rejected` 时保留终态结果不暂停。
3. **人工请求（`needs_user: yes`）：** 在应用该条消息后生成待办并暂停线程（即使同条消息关闭了任务，待办仍会显示）。暂停状态独立于任务状态，已暂停或终态线程的后续消息将被拒绝（`THREAD_CLOSED`）。
4. **授权不可通过留言板授予：** 留言板上的人工角色消息、复核通过或待办关闭均不构成执行授权。敏感操作（服务器配置、密钥、网络、测试集、训练启动、删除等）仍须人类在执行者会话中直接确认。
5. **后续新任务：** 经人类在会话中指示后，由 `claude_main` 发起新任务线程（可用 `supersedes` 关联旧的暂停/终态任务根），原暂停记录永不篡改。
