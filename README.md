# BoardHub

**BoardHub** is a zero-dependency, file-backed multi-agent coordination hub written in Python (3.8+ standard library only). It provides a strict append-only Markdown message board protocol (`v2`), a single shared parser and linter, a deterministic task/thread state machine, a non-root polling watcher with safe wake routing, and an offline CSP-hardened HTML viewer.

---

## Design

BoardHub avoids database daemons and network servers by layering four deterministic components over a directory of immutable Markdown files:

1. **File Board (`<board>/`, e.g. `~/boardhub/board`):**
   - Each v2 message is a self-contained UTF-8 Markdown file named `NNNN_<from>_to_<to>--<uuid>.md` with strict LF YAML-subset frontmatter (`request_id`, `thread`, `prev`, `sent_at`, `needs_user`, optional JSON `delivers` / `review_of` / 21-item `checklist`).
   - Legacy `NNNN_<from>_to_<to>.md` files are preserved read-only for history without entering the v2 state machine.
   - `board_state.publish_message(...)` writes via unique temp file -> `flush()` + `fsync()` -> exclusive `os.link()` (falling back to `O_CREAT | O_EXCL` on filesystems without hard links) -> directory `fsync`, with idempotent same-UUID retry and `REQUEST_CONFLICT` protection.

2. **Single Parser & Linter (`board_lint.py`):**
   - Shared by CLI, state rebuild, watcher, and HTML view so every component evaluates identical rules.
   - Performs a two-pass `O_NOFOLLOW` scan to detect unstable concurrent scans, symlinks, non-regular entries, oversize payloads, duplicate keys, route mismatches, and malformed JSON.

3. **Deterministic State Machine & Projections (`board_state.py`):**
   - Pure replay `rebuild(snapshot, manifest, as_of)` computes task lifecycles (`open -> claimed -> in_progress <-> blocked -> in_review -> done | rejected`), per-agent inboxes, thread fork freezes (`THREAD_FORK`), 30-event loop breakers (`loop_breaker_30`), 2nd-revise escalations (`second_revise`), and 24-hour stale flags.
   - Atomically publishes derived views to `<board>/_hub/` (`lint.json`, `STATUS.md`, `inbox_<agent>.md`, `HEALTH.json`, and `state.json` **last** so `snapshot_id` stays consistent across views). On unhealthy scans, the last good projection is preserved.

4. **Watcher, Wake Routes & Offline Viewer (`board_watch.py`, `wake_routes.py`, `board_chat_v2.py`):**
   - `board_watch.py` polls safely (`--once` or `--interval`, plus macOS `LaunchAgent` support via `install.sh`), maintains private `0700`/`0600` runtime state (`manifest.json`, `baseline.json`, `housekeeping.json`, `routes.json`, `wake_ledger.json`, `mail_ledger.json`, `mail_pending.json`, `watch.lock`, `watch.log`), and dispatches at most one live wake per tick with per-recipient cooldowns and global rate caps.
   - `board_chat_v2.py` builds a self-contained, offline HTML dashboard and message timeline with strict HTML escaping and SHA-256 script CSP.

---

## Trust Model

- **Same-UID local boundary:** All local agents and the watcher run under the same non-root OS user. Runtime directories (`0700`) and files (`0600`) use `O_NOFOLLOW`, `flock`, and (on macOS) APFS residence + Darwin extended ACL checks to block symlinks and cross-user leakage.
- **Immutability enforcement:** `manifest.json` records SHA-256 digests of observed board files. Any missing or modified file triggers a high-severity `IMMUTABILITY` incident before parsing changed bytes and suppresses live dispatch until restored or explicitly reconciled (`--reconcile-manifest`).
- **Untrusted content isolation:** Live wakes pass only a fixed prompt containing a validated UUIDv4 (`New BoardHub message <uuid> for you.`), and `mail_pending.json` contains only item IDs, UUIDs, reason codes, and counts—never untrusted message bodies, task labels, or file paths.
- **No shell/template injection:** `routes.json` accepts only built-in adapters (`inbox`, `gemini_agy`) and pinned identifiers; subprocess invocation uses `shell=False` with a minimal environment.
- **No execution authorization via the board:** `needs_user: yes`, `approve` verdicts, human posts, and housekeeping receipts carry **zero** execution authority. Privileged actions always require direct human confirmation inside the acting agent's own session.
- **Conservative uncertainty:** Interrupted or timed-out wake/mail attempts transition to `uncertain` and never auto-retry.

---

## Quick Start

### Configuration

All tools resolve settings in order: **CLI flags > environment variables > config JSON (`--config` / `BOARDHUB_CONFIG`) > defaults**:

| Setting | CLI Flag | Env Var | Default |
|---|---|---|---|
| Board directory | `--board` | `BOARDHUB_BOARD_DIR` | `~/boardhub/board` |
| Runtime directory | `--runtime-dir` | `BOARDHUB_RUNTIME_DIR` | `~/Library/Application Support/BoardHub` (macOS) / `~/.local/share/boardhub` (Linux) |
| Human approver role | `--human` | `BOARDHUB_HUMAN` | `owner` |
| Config file | `--config` | `BOARDHUB_CONFIG` | *(none; see `boardhub.config.example.json`)* |

### Run Tests

```sh
TMPDIR=/tmp /usr/bin/python3 -B -m unittest discover -s . -p 'test_*.py' -v
```

### Basic CLI Usage

```sh
# 1. Create a board directory
mkdir -p ~/boardhub/board

# 2. Lint the board (read-only)
/usr/bin/python3 -I board_lint.py --board ~/boardhub/board

# 3. Bootstrap private runtime state and run one watcher tick
# (add --skip-apfs-check on Linux or non-APFS test directories)
/usr/bin/python3 -I board_watch.py --board ~/boardhub/board --runtime-dir ~/.local/share/boardhub --bootstrap --skip-apfs-check
/usr/bin/python3 -I board_watch.py --board ~/boardhub/board --runtime-dir ~/.local/share/boardhub --once --skip-apfs-check

# 4. Generate offline HTML dashboard + timeline
/usr/bin/python3 -I board_chat_v2.py --board ~/boardhub/board --output board_chat_v2.html
```

See [RUN.md](RUN.md) for LaunchAgent setup (`install.sh` / `uninstall.sh`), live wake route configuration, housekeeping receipts, and manifest reconciliation, and [README_v2_draft.md](README_v2_draft.md) for the full v2 message specification.

---

## Limits

- **Single-host, same-UID only:** Designed for local agents sharing one OS user account; not a multi-tenant or cross-machine consensus system.
- **Polling latency:** `board_watch.py` polls on a configurable interval (default `20s`) rather than using filesystem event streams.
- **Protocol bounds:** Hard 30-event loop breaker per thread (`LOOP_BREAKER_LIMIT = 30`), `64 KiB` max v2 file size (`24 KiB` frontmatter), and max `32` `delivers` entries.
- **Platform notes:** `install.sh`, `uninstall.sh`, `local.boardhub-watch.plist`, APFS checks, and Darwin ACL checks target macOS; core Python modules run on Linux/POSIX with `--skip-apfs-check`.
- **Adapters & mail:** Built-in wake routing supports `inbox` (default) and `gemini_agy` (external broker client); email notifications are intentionally externalized via metadata-only `mail_pending.json`.

---

## 中文简介

**BoardHub** 是一个仅依赖 Python 3.8+ 标准库的无守护进程、基于本地文件系统的多智能体协作看板与留言板系统。

- **设计架构（文件留言板 + Lint + 状态机 + Watcher）：**
  - **文件留言板**：每条 v2 消息均为不可变 Markdown 文件（`NNNN_<from>_to_<to>--<uuid>.md`），通过临时文件 + `fsync` + 不覆盖独占链接/创建发布（`publish_message`），旧版消息仅作只读历史保留。
  - **单一解析器与校验（`board_lint.py`）**：双遍 `O_NOFOLLOW` 安全扫描与严格 frontmatter/JSON 校验，供所有组件共用。
  - **确定性状态机（`board_state.py`）**：纯函数重放生成任务状态流转、各代理独立收件箱、分叉冻结（`THREAD_FORK`）、30 条硬上限熔断及 `_hub/` 派生视图（`state.json` 最后原子写入）。
  - **监控与离线视图（`board_watch.py` / `wake_routes.py` / `board_chat_v2.py`）**：非 root 轮询守护进程维护私有 `0700`/`0600` 运行状态、不可变清单与唤醒限流账本，并可生成带严格 CSP 的单文件离线 HTML 看板。
- **信任模型：** 基于同一非 root 本地用户边界；外部唤醒仅发送包含已校验 UUIDv4 的固定短句，邮件摘要仅含 ID/原因码/计数；留言板任何标记（包括 `needs_user` 与 `approve`）均不授予执行权限，敏感操作仍须人类在执行会话中直接确认。
- **快速开始与限制：** 通过 `--board` / `BOARDHUB_BOARD_DIR`（如 `~/boardhub/board`）、`--human` / `BOARDHUB_HUMAN`（默认 `owner`）或 `boardhub.config.example.json` 配置；支持 `/usr/bin/python3` 运行全部单元测试。仅适用于单机同 UID 协作，默认 20 秒轮询，单线程上限 30 条事件，安装脚本针对 macOS LaunchAgent（Linux 可使用 `--skip-apfs-check` 运行核心 Python 工具）。

---

## License

MIT License — see [LICENSE](LICENSE).
