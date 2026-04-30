from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from claude_tg.config import Config
from claude_tg.hook_server import HookServer, PreToolUseRequest
from claude_tg.session_state import (
    Action, Ignore, Reject, ResolveApproval, ResolveDenyReason, ResolveReply,
    SessionState, State,
)
from claude_tg.stream_parser import (
    AssistantText, ToolUse, TurnEnd, parse_events,
)
from claude_tg.telegram_client import (
    CallbackUpdate, TelegramClient, TextUpdate,
)


log = logging.getLogger("claude_tg.session")


class Session:
    def __init__(
        self,
        config: Config,
        claude_argv: list[str],
        initial_prompt: str,
        telegram_base_url: str = "https://api.telegram.org",
        socket_path: Optional[str] = None,
    ) -> None:
        self.config = config
        self.claude_argv = claude_argv
        self.initial_prompt = initial_prompt
        self.telegram_base_url = telegram_base_url
        self.socket_path = socket_path or f"/tmp/claude-tg-{os.getpid()}.sock"
        self.exit_code: int | None = None

        self._state = SessionState()
        self._state_lock = asyncio.Lock()
        self._tg: TelegramClient | None = None
        self._hooks: HookServer | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._topic_id: int | None = None
        self._session_id = secrets.token_hex(3)
        self._current_approval: PreToolUseRequest | None = None
        self._temp_dir: Path | None = None
        self._last_activity_ts: float = 0.0

    def _bump_activity(self) -> None:
        self._last_activity_ts = time.time()

    async def run(self) -> None:
        self._tg = TelegramClient(
            token=self.config.telegram_bot_token,
            supergroup_id=self.config.telegram_supergroup_id,
            base_url=self.telegram_base_url,
        )
        self._hooks = HookServer(socket_path=self.socket_path)

        try:
            await self._hooks.start()
            cwd_name = Path.cwd().name or "cwd"
            self._topic_id = await self._tg.create_topic(
                f"session-{self._session_id} / {cwd_name}"
            )
            await self._tg.post_message(
                self._topic_id,
                f"\U0001f7e2 session started — {self.initial_prompt[:200]}",
            )
            await self._spawn_claude()
            # Run the three coroutines concurrently. When the stream reader
            # exits (Claude closed stdout), cancel the other two so run()
            # returns.
            self._bump_activity()
            stream_task = asyncio.create_task(self._read_stream())
            hooks_task = asyncio.create_task(self._serve_hooks())
            tg_task = asyncio.create_task(self._poll_telegram())
            heartbeat_task = asyncio.create_task(self._heartbeat())
            try:
                await stream_task
            finally:
                for t in (hooks_task, tg_task, heartbeat_task):
                    t.cancel()
                await asyncio.gather(
                    hooks_task, tg_task, heartbeat_task,
                    return_exceptions=True,
                )
        finally:
            await self._shutdown()

    async def _spawn_claude(self) -> None:
        # Build temp settings file with hook config
        td = Path(tempfile.mkdtemp(prefix=f"claude-tg-{self._session_id}-"))
        self._temp_dir = td
        settings_path = td / "settings.json"
        hook_cmd = f"{shutil.which('claude-tg-hook') or sys.executable + ' -m claude_tg.hook_script'} {self.socket_path} pre_tool_use"
        settings_path.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": hook_cmd}],
                }],
            },
            "permissions": {"defaultMode": "default"},
        }))

        argv = list(self.claude_argv)
        # Only real `claude` gets --settings / stream-json flags; stub subprocess
        # in tests runs without them.
        if argv and Path(argv[0]).name.startswith("claude"):
            argv += [
                "--settings", str(settings_path),
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--print",
                "--verbose",   # required by Claude Code 2.1.x when combining --print with stream-json
            ]

        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "STUB_HOOK_CMD": hook_cmd},
        )
        # Write initial prompt
        await self._write_user_message(self.initial_prompt)

    async def _write_user_message(self, text: str) -> None:
        assert self._proc and self._proc.stdin
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": text},
        }) + "\n"
        self._proc.stdin.write(msg.encode())
        await self._proc.stdin.drain()

    async def _read_stream(self) -> None:
        assert self._proc and self._proc.stdout and self._tg and self._topic_id
        async def lines():
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    return
                yield line.decode(errors="replace")

        buffer: list[str] = []

        async def flush_buffer():
            if buffer:
                text = "".join(buffer).strip()
                buffer.clear()
                if text:
                    await self._tg.post_message(self._topic_id, text)
                    self._bump_activity()

        async def process_events():
            async for line in lines():
                for ev in parse_events([line]):
                    if isinstance(ev, AssistantText):
                        buffer.append(ev.text)
                        if "\n" in ev.text:
                            await flush_buffer()
                    elif isinstance(ev, ToolUse):
                        await flush_buffer()
                        preview = json.dumps(ev.input)[:500]
                        await self._tg.post_message(
                            self._topic_id, f"\U0001f527 Running {ev.name}: `{preview}`"
                        )
                        self._bump_activity()
                    elif isinstance(ev, TurnEnd):
                        await flush_buffer()
                        async with self._state_lock:
                            self._state.on_turn_end()
                        self._bump_activity()

        await process_events()
        # stdout closed -> Claude exited
        if self._proc:
            self.exit_code = await self._proc.wait()

    async def _serve_hooks(self) -> None:
        assert self._hooks and self._tg and self._topic_id
        while True:
            try:
                req = await self._hooks.next_pre_tool_use()
            except asyncio.CancelledError:
                return
            preview = json.dumps(req.tool_input)
            if len(preview) > 500:
                dump_dir = Path("/tmp") / f"claude-tg-{self._session_id}"
                dump_dir.mkdir(exist_ok=True, mode=0o700)
                idx = len(list(dump_dir.glob("tool-*.txt")))
                dump_path = dump_dir / f"tool-{idx}.txt"
                dump_path.write_text(preview)
                os.chmod(dump_path, 0o600)
                preview = preview[:500] + f"\n... truncated; full payload: {dump_path}"
            mid = await self._tg.post_approval(
                topic_id=self._topic_id,
                tool_name=req.tool_name,
                preview=preview,
            )
            async with self._state_lock:
                assert self._current_approval is None, "overlapping approvals not supported"
                self._state.on_pre_tool_use(approval_message_id=mid)
                self._current_approval = req
            self._bump_activity()

    async def _heartbeat(self) -> None:
        assert self._tg and self._topic_id
        last_topic_name: str | None = None
        while True:
            await asyncio.sleep(max(self.config.heartbeat_interval_sec, 1))
            async with self._state_lock:
                if self._state.state == State.ENDED:
                    return
                activity = self._last_activity_ts
            idle = (time.time() - activity) > self.config.idle_threshold_sec
            icon = "\U0001f7e1" if idle else "\U0001f7e2"
            name = f"session-{self._session_id} {icon}"
            if name != last_topic_name:
                await self._tg.set_topic_name(self._topic_id, name)
                last_topic_name = name

    async def _poll_telegram(self) -> None:
        assert self._tg
        async for update in self._tg.poll_updates():
            if update.from_user_id not in self.config.allowed_user_ids:
                continue
            # Ignore text updates from other topics (e.g. other concurrent sessions).
            if isinstance(update, TextUpdate) and update.topic_id != self._topic_id:
                continue
            if isinstance(update, TextUpdate):
                text = update.text.strip()
                if text == "/stop":
                    # Do NOT hold the state lock while waiting for the process
                    # to exit; _read_stream may need the lock to flush events.
                    await self._stop_gracefully()
                    return
                async with self._state_lock:
                    if text == "/cancel":
                        action = self._state.on_cancel()
                    else:
                        action = self._state.on_text(update.from_user_id, text)
                    await self._apply_action(action, update)
            elif isinstance(update, CallbackUpdate):
                await self._tg.answer_callback(update.callback_query_id)
                mid, _, kind = update.data.partition(":")
                try:
                    mid_int = int(mid)
                except ValueError:
                    continue
                should_edit_for_deny_tell = False
                async with self._state_lock:
                    action = self._state.on_callback(mid_int, kind)
                    if (
                        kind == "deny_tell"
                        and self._state.state == State.WAITING_DENY_REASON
                    ):
                        should_edit_for_deny_tell = True
                    await self._apply_action(action, update)
                if should_edit_for_deny_tell:
                    await self._tg.edit_message_text(
                        mid_int,
                        "✏️ send a short reason or instruction (or /cancel)",
                    )

    async def _apply_action(
        self, action: Action,
        update: TextUpdate | CallbackUpdate,
    ) -> None:
        if isinstance(action, ResolveApproval):
            if self._current_approval:
                self._current_approval.resolve(
                    decision=action.decision, reason=action.reason
                )
                self._current_approval = None
            if isinstance(update, TextUpdate):
                await self._tg.react(update.message_id, "✅")
            self._bump_activity()
        elif isinstance(action, ResolveDenyReason):
            if self._current_approval:
                self._current_approval.resolve(decision="deny", reason=action.reason)
                self._current_approval = None
            if isinstance(update, TextUpdate):
                await self._tg.react(update.message_id, "✅")
            self._bump_activity()
        elif isinstance(action, ResolveReply):
            if isinstance(update, TextUpdate):
                await self._tg.react(update.message_id, "✅")
            await self._write_user_message(action.text)
            self._bump_activity()
        elif isinstance(action, Reject):
            await self._tg.post_message(self._topic_id, action.message)
        elif isinstance(action, Ignore):
            pass

    async def _stop_gracefully(self) -> None:
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
        if self._proc:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.send_signal(signal.SIGTERM)
                await self._proc.wait()
            self.exit_code = self._proc.returncode
        async with self._state_lock:
            self._state.on_end()

    async def _shutdown(self) -> None:
        async with self._state_lock:
            self._state.on_end()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass
        if self._tg and self._topic_id:
            try:
                exit_note = f"\U0001f6d1 session ended (exit {self.exit_code})"
                await self._tg.post_message(self._topic_id, exit_note)
                await self._tg.set_topic_name(
                    self._topic_id, f"✓ session-{self._session_id}"
                )
                await self._tg.close_topic(self._topic_id)
            except Exception as e:
                log.warning("shutdown telegram notify failed: %s", e)
        if self._tg:
            await self._tg.aclose()
        if self._hooks:
            await self._hooks.stop()
        # Clean up per-session payload dump dir if we created one.
        dump_dir = Path("/tmp") / f"claude-tg-{self._session_id}"
        if dump_dir.exists():
            shutil.rmtree(dump_dir, ignore_errors=True)
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
