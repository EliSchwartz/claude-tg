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

Telegram was chosen after considering Slack, Discord, ntfy, email, and a self-hosted web app. Rationale: native mobile push, writable channel, inline-keyboard approval buttons, forum topics for per-session isolation, and zero self-hosted infrastructure beyond the wrapper. The user confirmed no policy concerns with prompts and tool-call details transiting Telegram's servers.

### Per-session isolation: forum topics

A single "Claude Sessions" supergroup in Telegram is configured once with forum topics enabled. Each session creates a new topic. The user sees one chat in their Telegram sidebar; each concurrent session is a separate thread within it.

### Hook-based control

Claude Code's `PreToolUse` hook is the documented, stable interface for gating tool use. The hook script is a thin stub that forwards requests to the wrapper over a unix-domain socket; the wrapper asks Telegram, blocks on the reply, and returns the decision. Turn-end is detected from stream-json output (and optionally the `Stop` hook as an implementation choice).

The wrapper overrides the user's `skipAutoPermissionPrompt: true` and `defaultMode: auto` settings via a temporary settings file passed with `--settings`, so `PreToolUse` actually fires for every tool call. (If `--settings` does not take precedence, the wrapper merges manually.)

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

Validates the bot is a member of the supergroup and the group has forum topics enabled; fails fast with a clear message otherwise.

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

Handles exponential backoff on transient errors (max 30 s, 5 retries).

### `hook_server.py`

Serves a unix-domain socket at `/tmp/claude-tg-<pid>.sock`. Two endpoints:

- `pre_tool_use(tool_name, tool_input) -> {decision, reason?}`
- `stop(last_assistant_text) -> {user_reply}` (if we use the Stop hook — see "Turn-end detection" below)

Each call blocks until the user responds via Telegram. Requests are queued strict-FIFO; concurrent hooks shouldn't happen in practice but the queue guarantees ordering.

### `hook_script.py`

Tiny stub installed as the hook command in the temp settings file. Reads hook JSON from stdin, opens the unix socket, forwards the request, writes the response back on stdout in the format Claude expects. Kept minimal: Claude may invoke it many times per session.

### `session.py`

The orchestrator. Owns the Claude subprocess, hook server, Telegram client, and topic lifecycle. Runs four concurrent asyncio tasks:

1. **Stream reader** — reads Claude's stream-json stdout; relays text to Telegram (debounced, chunked on newlines) and "🔧 Running <tool>" lines on tool-use events.
2. **Hook server** — accepts hook requests, posts to Telegram, awaits resolution.
3. **Telegram poller** — long-polls updates, dispatches replies to the oldest pending hook request or (if none pending) treats as next-turn input and writes to Claude's stdin.
4. **Heartbeat** — every `heartbeat_interval_sec`, updates the topic name suffix: `🟢` live, `🟡` idle (no activity for `idle_threshold_sec`). On shutdown, sets `🔴 ended` or `✓ ended`.

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
  -> post approval card:
       "⚠️ Approve tool: Bash
        ```rm -rf build/```
        [Approve] [Deny] [Deny+tell]"
  -> block on Future keyed by message_id
  -> user taps Approve on phone -> callback query via long-poll
  -> poller resolves Future with {decision: "approve"}
  -> react 👀 on the callback acknowledgement
  -> hook_server returns to hook_script -> Claude proceeds
```

### Turn-end reply

```
Claude finishes a turn: "I've refactored X. Want me to also do Y?"
  -> wrapper has already streamed the text to Telegram as it arrived
  -> turn-end detected (see "Turn-end detection" below)
  -> wait for user reply in topic
  -> user types reply
  -> poller resolves pending request
  -> react ✅ on the user's message
  -> write {"type":"user","message":{"content":[{"type":"text","text":"<reply>"}]}}
     to Claude's stdin
  -> Claude starts the next turn
```

### Streaming assistant text

As stream-json events arrive, the wrapper posts `assistant` text blocks to Telegram in near-real-time. Debounced (100 ms) and chunked on newline boundaries to avoid message-flood. Tool-use events post a compact "🔧 Running Bash: `…`" line.

### Heartbeat and shutdown

- Every 30 s, the topic name suffix updates: `🟢` live, `🟡` after 2 min of no activity.
- On Claude exit (normal or crash): post "🛑 session ended (exit N)" (with tail of stderr on crash), rename topic to `✓ session-a3f2`, close socket, exit with Claude's exit code.
- On SIGTERM/SIGINT to the wrapper: clean shutdown in the same shape.

### Turn-end detection — implementation note

Two viable approaches; the spec does not pin one:

- **A.** Detect turn-end from the stream-json event stream (e.g., the `result` or end-of-turn marker), then prompt Telegram and write the reply to Claude's stdin. No Stop hook needed.
- **B.** Use the `Stop` hook to pause; the hook server blocks on the Telegram reply; on unblock, the wrapper writes the reply to Claude's stdin.

Both end by writing the reply to Claude's stdin via stream-json input mode. The implementer picks based on which event is more reliable in practice.

## Error Handling and Edge Cases

| Condition                                      | Behavior                                                                                                                                              |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Telegram unreachable (transient)               | Backoff; retry up to 30 s × 5.                                                                                                                        |
| Telegram unreachable (after retries)           | Apply `on_telegram_failure` config (default `deny`); log to stderr.                                                                                   |
| User reply timeout                             | If `reply_timeout_sec > 0`, deny the tool / exit the session with "⏱ timed out after N s".                                                            |
| User sends `/cancel` in topic                  | Deny the pending request; post "❌ cancelled by user".                                                                                                 |
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
- Tool input previews and full payloads are truncated/stored locally; the wrapper never uploads arbitrary workspace files to Telegram without the user's approval of a tool call that would read them.

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

## Open Questions / Risks

- **Settings precedence.** `--settings` is assumed to override user settings for hook configuration and permission mode. If precedence is the other way, the wrapper merges the user's settings and writes a combined temp file. To be verified during implementation.
- **Turn-end signal.** Either `Stop` hook or stream-json end-of-turn marker — implementer picks based on reliability testing.
- **Stream-json input format.** The exact JSON shape for writing a user message to Claude's stdin must be confirmed from Claude Code's current documentation during implementation.
- **Hook payload shape.** The `PreToolUse` hook input/output JSON is assumed to match the documented shape; to be verified against the version of Claude Code in use.
