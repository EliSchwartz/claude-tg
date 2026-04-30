# claude-tg: Remote Control for Claude Code via Telegram

**Date:** 2026-04-30
**Status:** Design

## Problem

The user runs Claude Code under an API-key-authenticated endpoint (LiteLLM proxy over an Anthropic-compatible API). The built-in `remote_control` feature in Claude Code does not work in this configuration. The user wants to receive approval requests and clarification questions on their phone and respond to them remotely while a Claude session runs on their workstation.

Requirements:

- One Telegram chat per Claude session, isolated from other sessions.
- Phone-friendly approval UX (tap to approve/deny tool calls).
- Phone-friendly clarification UX (type a reply, it becomes Claude's next prompt).
- Confirmation that the running session received the reply (no "did it arrive?" guessing).
- Low-friction session startup from the terminal.
- Support for up to ~5 concurrent sessions.

## Scope

In scope:

- A wrapper CLI (`claude-tg`) that the user launches instead of `claude`.
- Per-session Telegram forum topic in a pre-configured supergroup.
- Real-time relay of assistant text and tool-use activity to the topic.
- `PreToolUse` hook that posts approval cards to Telegram and blocks until the user responds.
- Turn-boundary detection that posts to Telegram and blocks until the user replies, then injects the reply as Claude's next prompt.
- Read-receipt reaction on user replies; live/idle indicator in topic title; session-end marker.
- Minimal per-user auth (allowlist of Telegram user IDs).

Out of scope (v1):

- Starting a session from Telegram (requires an always-on daemon; deferred).
- Resuming a session across workstation reboots.
- Multi-user collaboration on the same session.
- Web UI or native mobile client.

## Architecture

`claude-tg` is a Python wrapper the user runs instead of `claude`. It launches Claude Code as a subprocess in stream-json mode, installs hooks that call back into the wrapper, and relays messages between Claude and a Telegram forum topic.

```
┌──────────────┐      stream-json stdout      ┌──────────────┐
│  claude-tg   │ ◄───────────────────────────  │  claude CLI  │
│  (wrapper)   │                                │  (subprocess)│
│              │  stdin: stream-json prompts   │              │
│              │  ────────────────────────────►│              │
│              │                                └──────────────┘
│              │            ▲
│              │            │ hooks (PreToolUse, Stop)
│              │            │ via unix-domain socket
│              │  ◄─────────┘
│              │
│              │  Telegram Bot API (long-poll)
│              │  ◄──────────────────────────►  Telegram servers
└──────────────┘                                 (one forum topic
                                                  per session)
```

### Transport choice: Telegram

Telegram was chosen after considering Slack, Discord, ntfy, email, and a self-hosted web app. Rationale: native mobile push, writable channel, inline-keyboard approval buttons, forum topics for per-session isolation, and no self-hosted server beyond the wrapper itself (the wrapper must remain running alongside Claude for the duration of the session). The user confirmed no policy concerns with prompts and tool-call details transiting Telegram's servers.

### Per-session isolation: forum topics

A single "Claude Sessions" supergroup in Telegram is configured once with Topics enabled (Telegram's UI term for forum topics). Each session creates a new topic. The user sees one chat in their Telegram sidebar; each concurrent session is a separate thread within it.

### Hook-based control

Claude Code's `PreToolUse` hook is the documented, stable interface for gating tool use. The hook script is a thin stub that forwards requests to the wrapper over a unix-domain socket; the wrapper asks Telegram, blocks on the reply, and returns the decision.

The wrapper overrides the user's `skipAutoPermissionPrompt: true` and `defaultMode: auto` settings via a temporary settings file passed with `--settings`, so `PreToolUse` actually fires for every tool call. (If `--settings` does not take precedence, the wrapper merges manually.)

### Turn-end detection: default to stream-json

The design prefers detecting turn-end from the stream-json output stream (e.g., the `result` event or equivalent end-of-turn marker). The `Stop` hook is used only as a fallback if stream-json does not provide a reliable end-of-turn signal in practice. Blocking inside `Stop` is avoided unless testing confirms the hook surface is intended for interactive continuation — otherwise the wrapper detects turn-end from the output stream and writes the next prompt to Claude's stdin without involving `Stop`.

### Critical protocol assumption: multi-turn stdin

The wrapper assumes Claude Code in `--input-format stream-json --output-format stream-json` accepts multiple sequential user messages over stdin from a parent process — not just the initial prompt. This is the highest-risk assumption in the design. It **must be validated before implementing the Telegram flow**. See the Implementation Validation Checklist below. If it does not hold, the architecture changes: either use `Stop`-hook blocking as the primary control point (not just a fallback), or run Claude under a PTY and drive it interactively instead of via stream-json stdin.

## Components

Five small modules, each with one job.

### `config.py`

Loads `~/.config/claude-tg/config.toml`:

```toml
telegram_bot_token = "..."
telegram_supergroup_id = -1001234567890
allowed_user_ids = [12345678]

# optional
reply_timeout_sec = 0          # 0 = no timeout
on_telegram_failure = "deny"   # "deny" | "approve" | "ask_cli"
heartbeat_interval_sec = 30
idle_threshold_sec = 120
```

Validates the bot is a member of the supergroup and the group has Topics enabled; fails fast with a clear message otherwise.

### `telegram_client.py`

Thin async wrapper around the Telegram Bot API:

- `create_topic(name) -> topic_id`
- `post_message(topic_id, text) -> message_id` (chunks at 4000 chars)
- `post_approval(topic_id, tool_name, preview, full_payload_path) -> message_id`
  — message has an inline keyboard with Approve / Deny / Deny+tell
- `react(message_id, emoji)`
- `set_topic_name(topic_id, name)` (for live/idle/ended indicators)
- `close_topic(topic_id)` / `archive_topic(topic_id)`
- `poll_updates()` — async generator yielding incoming text messages and callback queries

Handles exponential backoff on transient errors: up to 5 retries, capped at 30 s between retries.

The client also gracefully degrades if the bot lacks some permissions (see "Forum topic permissions and fallback" below): `react`, `set_topic_name`, and `close_topic` log-and-continue if Telegram rejects them; only `create_topic` and `post_message` are considered fatal.

### `hook_server.py`

Serves a unix-domain socket at `/tmp/claude-tg-<pid>.sock`. Two endpoints:

- `pre_tool_use(tool_name, tool_input) -> {decision, reason?}`
- `stop(last_assistant_text) -> {user_reply}` (if we use the Stop hook — see "Turn-end detection" below)

Each call blocks until the user responds via Telegram. Requests are queued strict-FIFO; concurrent hooks shouldn't happen in practice but the queue guarantees ordering.

### `hook_script.py`

Tiny stub installed as the hook command in the temp settings file. Reads hook JSON from stdin, opens the unix socket, forwards the request, writes the response back on stdout in the format Claude expects. Kept minimal: Claude may invoke it many times per session. Socket connection has a short timeout (2 s) — if the wrapper is gone or unresponsive, the stub returns a default `deny` so Claude never hangs indefinitely on a dead wrapper.

### `session.py`

The orchestrator. Owns the Claude subprocess, hook server, Telegram client, and topic lifecycle. Runs four concurrent asyncio tasks:

1. **Stream reader** — reads Claude's stream-json stdout; relays text to Telegram (debounced, chunked on newlines) and "🔧 Running <tool>" lines on tool-use events. Detects end-of-turn and transitions state to `WAITING_USER_REPLY`.
2. **Hook server** — accepts hook requests; on `PreToolUse`, transitions state to `WAITING_TOOL_APPROVAL`, posts to Telegram, awaits resolution, then returns to previous state.
3. **Telegram poller** — long-polls updates and dispatches them according to the current session state (see "State Machine" below). Approvals and plain-text replies are handled distinctly, not through a single unified queue.
4. **Heartbeat** — every `heartbeat_interval_sec`, updates the topic name suffix: `🟢` live, `🟡` idle (no activity for `idle_threshold_sec`). On shutdown, sets `🔴 ended` or `✓ ended`.

The session owns a single `state` variable (`RUNNING | WAITING_TOOL_APPROVAL | WAITING_USER_REPLY | ENDED`) guarded by an asyncio lock. All state transitions go through a single method so race conditions between the stream reader and the hook server are explicit.

### Entry point — `claude_tg/__main__.py`

`claude-tg [prompt] [args...]` mirrors `claude`; unknown args pass through to Claude.

## Data Flow

### Startup

```
user $ claude-tg "refactor the auth module"
  -> load config
  -> create forum topic "session-a3f2 / cc_remote" in supergroup
  -> start hook_server on /tmp/claude-tg-<pid>.sock
  -> write temp settings file with hook config and default permissions
  -> spawn: claude --settings <temp> \
                   --output-format stream-json \
                   --input-format stream-json \
                   "refactor the auth module"
  -> post "🟢 session started - refactor the auth module" to topic
```

### Tool approval (PreToolUse)

```
Claude wants to run: Bash("rm -rf build/")
  -> PreToolUse hook fires -> hook_script -> unix socket -> hook_server
  -> state := WAITING_TOOL_APPROVAL
  -> post approval card:
       "⚠️ Approve tool: Bash
        ```rm -rf build/```
        [Approve] [Deny] [Deny+tell]"
  -> block on Future keyed by message_id
  -> user taps Approve -> callback query via long-poll
  -> acknowledge the callback; react 👀 on the approval message
  -> poller resolves Future with {decision: "approve"}
  -> state := RUNNING
  -> hook_server returns to hook_script -> Claude proceeds
```

### Approval button behavior

- **Approve** — hook returns `{"decision": "approve"}`. Claude runs the tool.
- **Deny** — hook returns `{"decision": "deny", "reason": "denied by user"}`. Claude sees the denial and continues.
- **Deny+tell** — bot edits the approval card to prompt "✏️ send a reason or instruction"; state moves to a short-lived `WAITING_DENY_REASON`. The next plain-text message from the user becomes the denial reason. Hook returns `{"decision": "deny", "reason": "<user text>"}`. If the user sends `/cancel` instead, the denial falls back to `"denied by user"`. The user's text may also be relevant as guidance for the next turn; after the deny is returned to Claude, Claude's natural response to "I denied that because <reason>" is to adjust course, so we don't need to separately inject the text as a user message.

### Turn-end reply

```
Claude finishes a turn: "I've refactored X. Want me to also do Y?"
  -> wrapper has already streamed the text to Telegram as it arrived
  -> stream reader detects end-of-turn marker in stream-json
  -> state := WAITING_USER_REPLY
  -> wait for user text reply in the topic
  -> user types reply
  -> poller dispatches to pending reply waiter
  -> react ✅ on the user's message
  -> write {"type":"user","message":{"content":[{"type":"text","text":"<reply>"}]}}
     to Claude's stdin                 (schema to be verified; see Validation Checklist)
  -> state := RUNNING
  -> Claude starts the next turn
```

### Streaming assistant text

As stream-json events arrive, the wrapper posts `assistant` text blocks to Telegram in near-real-time. Debounced (100 ms) and chunked on newline boundaries to avoid message-flood. Tool-use events post a compact "🔧 Running Bash: `…`" line.

### Heartbeat and shutdown

- Every 30 s, the topic name suffix updates: `🟢` live, `🟡` after 2 min of no activity.
- On Claude exit (normal or crash): post "🛑 session ended (exit N)" (with tail of stderr on crash), rename topic to `✓ session-a3f2`, close socket, exit with Claude's exit code.
- On SIGTERM/SIGINT to the wrapper: clean shutdown in the same shape.

## State Machine

The session is in exactly one state at a time. All Telegram events and all internal signals are interpreted through the current state.

```text
State: RUNNING
- Stream reader relays assistant text and tool-use to Telegram.
- PreToolUse hook → transition to WAITING_TOOL_APPROVAL.
- End-of-turn marker → transition to WAITING_USER_REPLY.
- Telegram text messages: rejected with "⚠ session is working — wait for the next turn or use /cancel".
- Callback buttons: ignored (stale; from a resolved approval).
- /cancel: post "nothing pending"; stay in RUNNING.
- /stop: request graceful shutdown (see below).

State: WAITING_TOOL_APPROVAL
- Approve/Deny callback on the pending approval: resolves the hook future; transition to RUNNING (or WAITING_DENY_REASON for Deny+tell).
- Telegram text messages: post "⏸ an approval is pending — respond to it first"; do not inject.
- Stale callbacks (wrong message_id): ignored.
- /cancel: resolves hook with deny, reason="cancelled by user"; back to RUNNING.
- /stop: resolves hook with deny then shuts down.

State: WAITING_DENY_REASON (short-lived substate of WAITING_TOOL_APPROVAL)
- Next plain text from allowlisted user: becomes the denial reason; hook resolves with deny+reason; → RUNNING.
- Callback on that approval card: ignored (card is now "awaiting reason").
- /cancel: deny with reason="denied by user"; → RUNNING.

State: WAITING_USER_REPLY
- First plain text from allowlisted user: reacted with ✅, written to Claude's stdin; → RUNNING.
- Callback buttons: ignored.
- /cancel: no pending approval → bot replies "nothing to cancel; use /stop to end the session".
- /stop: graceful shutdown.

State: ENDED
- All Telegram input is ignored or answered once with "🛑 session ended".
- Topic name set to ✓ / 🔴 ended; no further changes.
```

**Commands:**
- `/cancel` — cancels the currently pending approval or deny-reason prompt. No-op in RUNNING or WAITING_USER_REPLY (bot responds accordingly).
- `/stop` — terminates the Claude session gracefully: writes EOF / closes stdin, waits briefly, then SIGTERMs the subprocess if needed; posts the session-end marker; → ENDED.

## Error Handling and Edge Cases

| Condition                                      | Behavior                                                                                                                                              |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Telegram unreachable (transient)               | Backoff; retry up to 30 s × 5.                                                                                                                        |
| Telegram unreachable (after retries)           | Apply `on_telegram_failure` config (default `deny`); log to stderr.                                                                                   |
| User reply timeout                             | If `reply_timeout_sec > 0`, deny the tool / exit the session with "⏱ timed out after N s".                                                            |
| `/cancel` during WAITING_TOOL_APPROVAL         | Deny pending request, reason="cancelled by user"; → RUNNING; post "❌ cancelled".                                                                      |
| `/cancel` during WAITING_DENY_REASON           | Deny with default reason ("denied by user"); → RUNNING.                                                                                                |
| `/cancel` during RUNNING or WAITING_USER_REPLY | Bot replies "nothing to cancel; use /stop to end the session".                                                                                         |
| `/stop`                                        | Graceful shutdown: close Claude's stdin, wait briefly, SIGTERM if needed, post session-end marker.                                                     |
| Claude subprocess crash                        | Post "🛑 session ended unexpectedly (exit N)" with stderr tail; rename topic; exit with Claude's code.                                                |
| Wrapper crash or kill                          | Hook script can't reach socket; returns default `deny` to Claude; Claude keeps running but can't use tools.                                           |
| Unrelated text arrives while approval pending  | Bot replies "⏸ an approval is pending — respond to it first"; text not injected.                                                                      |
| Large tool input (>500 chars)                  | Inline preview truncated at 500 chars; full payload saved to `/tmp/claude-tg-<session>/tool-<n>.txt` and referenced in the approval card.             |
| Message >4096 chars (Telegram limit)           | Chunk at 4000 chars across multiple messages.                                                                                                         |
| Non-allowlisted Telegram user                  | Silently ignore and log.                                                                                                                              |
| Messages from chats other than the supergroup  | Ignore.                                                                                                                                               |
| Missing or invalid config at startup           | Fail fast before spawning Claude; print a clear message with the config path and required fields.                                                     |
| User's `skipAutoPermissionPrompt: true`        | Overridden in the temp settings file so `PreToolUse` fires.                                                                                           |
| User's `defaultMode: "auto"`                   | Overridden to `"default"` in the temp settings file.                                                                                                  |
| Bot can't create forum topics                  | Fatal at startup; print clear remediation (enable Topics on the supergroup, give bot Manage Topics permission).                                        |
| Bot can't rename topics                        | Log warning; liveness indicator disabled; session otherwise works.                                                                                     |
| Bot can't react to messages                    | Log warning; reactions disabled; session otherwise works (user sees Claude's response arrive as implicit confirmation).                                |
| Topics disabled on supergroup mid-session      | Existing topic continues as a plain thread if possible; otherwise fall back to posting in the main group with a session-id prefix.                     |

## Read-Receipt and Liveness UX

- Every user reply gets an emoji reaction (✅) the moment the wrapper injects it into Claude — per-message "delivered" ack.
- Every approval callback gets 👀 when the decision is returned to Claude.
- Topic name suffix shows session liveness: `🟢` live, `🟡` idle, `🔴`/`✓` ended.
- Session-end marker ("🛑 session ended …") is always posted so the topic has a clear terminal state.

## Security

- Bot token and supergroup ID live in a user-only-readable config file (`chmod 600 ~/.config/claude-tg/config.toml`).
- Every incoming Telegram update is checked against `allowed_user_ids`; unknown senders are silently ignored and logged.
- Supergroup ID is pinned; messages from other chats are ignored even from allowlisted users.
- Unix socket path includes the wrapper PID and lives in `/tmp` with `0600` permissions.
- The wrapper does not proactively upload workspace files. However, assistant output, tool previews, and approval cards may contain sensitive workspace content (file paths, code snippets, command arguments, URLs, and potentially secrets that appear in diffs or configs). Users should treat the configured Telegram supergroup as trusted infrastructure.

## Testing

**Unit tests.**

- `telegram_client` — mock HTTP; verify API calls, chunking, retry, callback parsing.
- `hook_server` — exercise the socket protocol with a fake client; verify blocking and FIFO.
- `config` — valid/invalid TOML loading.
- Stream-json event parsing — fixture JSON lines in, mock-telegram calls out.

**Integration tests (no real Telegram).**

- Fake Telegram HTTP server locally; script scenarios end-to-end:
  - approval → fake callback → Claude sees decision
  - turn-end → fake text reply → prompt written to Claude's stdin
  - network dropout → recovery
  - session exit → topic renamed
- "Claude" is a stub subprocess emitting canned stream-json and reading stdin.

**Manual smoke test (not in CI).**

- `scripts/smoke.sh` against a real test bot + test supergroup with a trivial prompt (`echo hello`). README checklist covers: approval card, Approve button, reaction, turn-end roundtrip, heartbeat, session-end marker.

TDD approach: write fixture-based tests for each stream-json event type and each hook payload shape before implementing handlers.

## Configuration Summary

`~/.config/claude-tg/config.toml`:

```toml
telegram_bot_token = "123456:ABC..."
telegram_supergroup_id = -1001234567890
allowed_user_ids = [12345678]
reply_timeout_sec = 0
on_telegram_failure = "deny"
heartbeat_interval_sec = 30
idle_threshold_sec = 120
```

## Implementation Validation Checklist

The following must be validated **before** building the Telegram relay. Each is a hard gate: if any fails, the architecture needs to be revisited.

1. **Multi-turn stdin.** Can Claude Code in `--input-format stream-json --output-format stream-json` accept multiple sequential user messages from a parent process via stdin after the initial prompt? This is the highest-risk assumption. If no, switch the design to: (a) Stop-hook-blocking as the primary control point, or (b) a PTY-driven interactive wrapper.
2. **End-of-turn event.** What exact stream-json event marks end-of-turn in the current version of Claude Code? Verify it's reliably emitted before the process would block awaiting new input.
3. **Settings precedence.** Does `claude --settings <temp>` override user-level `settings.json` for `hooks` and `permissions`? If not, the wrapper merges user settings into the temp file manually.
4. **PreToolUse hook schema.** What is the exact input JSON Claude sends on stdin to the hook command, and what output JSON is expected on stdout to approve / deny? Verify against the installed version.
5. **Telegram bot permissions.** Can the bot create, rename, and post in forum topics, and react to messages, inside the configured supergroup? Identify which permissions are required vs. nice-to-have.

A small exploratory script (`scripts/probe.py`) runs checks 1–4 against the local `claude` binary and prints a report. A similar helper runs check 5 against the configured bot and supergroup.

## Risks

- **Hook UI stability.** Claude Code's hook schema may change between versions. The wrapper should log the Claude version at startup and fail fast if the hook payload shape doesn't match expectations.
- **Stream-json format stability.** Same concern; mitigated by probing at startup.
- **Telegram rate limits.** Real-time streaming of assistant text could hit Bot API rate limits. The 100 ms debounce and newline-chunking are the first line of defense; if that proves insufficient, add a minimum-interval throttle.
- **Long-running wrapper reliability.** The wrapper must remain running for the lifetime of the session; if the user closes the terminal, the session ends. Document this clearly.
