"""Integration tests using fake Telegram + a stub Claude subprocess.

The stub Claude is a short Python program that reads stream-json from stdin
and emits stream-json on stdout. It simulates: one assistant message, one
tool_use, an approval wait, then a result event.
"""

import asyncio
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from claude_tg.config import Config
from claude_tg.session import Session
from tests.fake_telegram import FakeTelegram


STUB_CLAUDE = textwrap.dedent("""
    import json, os, subprocess, sys, time

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    # Turn 1: text, tool_use (hook will fire), then wait for approval result
    # to come back via... actually, in stream-json mode the hook handles
    # approval. We just need to emit the events Claude would emit.
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "thinking..."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
    ]}})

    # Simulate the hook being invoked by the parent: we check for a hook
    # settings file via env var and invoke it ourselves.
    hook_cmd = os.environ.get("STUB_HOOK_CMD", "")
    if hook_cmd:
        result = subprocess.run(
            hook_cmd.split(),
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": "ls"}}),
            capture_output=True, text=True, timeout=30,
        )
        # We don't actually run the tool; we just wait for the hook.

    # End of turn.
    emit({"type": "result"})

    # Read next user message
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") == "user":
            emit({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "done"},
            ]}})
            emit({"type": "result"})
            break
""").strip()


@pytest.fixture
async def fake_tg():
    f = FakeTelegram()
    base = await f.start()
    try:
        yield f, base
    finally:
        await f.stop()


async def test_approval_then_reply_happy_path(fake_tg, tmp_path):
    f, base = fake_tg
    stub_path = tmp_path / "stub_claude.py"
    stub_path.write_text(STUB_CLAUDE)

    cfg = Config(
        telegram_bot_token="TOKEN",
        telegram_supergroup_id=-100,
        allowed_user_ids=[42],
    )

    session = Session(
        config=cfg,
        claude_argv=[sys.executable, str(stub_path)],
        telegram_base_url=base,
        socket_path=str(tmp_path / "s.sock"),
        initial_prompt="go",
    )

    # Register callback that auto-approves after the approval card is sent.
    async def on_send(body):
        if "reply_markup" in body:
            f.next_message_id += 1
            mid = f.next_message_id
            # fire an approve callback after the message is "seen"
            async def push():
                await asyncio.sleep(0.05)
                f.push_update({
                    "update_id": 1,
                    "callback_query": {
                        "id": "cbq1", "from": {"id": 42},
                        "data": f"{mid}:approve",
                        "message": {"message_id": mid, "chat": {"id": -100}},
                    },
                })
                # Then push a reply for the turn-end wait.
                await asyncio.sleep(0.2)
                f.push_update({
                    "update_id": 2,
                    "message": {"message_id": 99, "from": {"id": 42},
                                "chat": {"id": -100},
                                "message_thread_id": 51,
                                "text": "continue"},
                })
            asyncio.create_task(push())
            return {"message_id": mid, "chat": {"id": -100}}
        f.next_message_id += 1
        return {"message_id": f.next_message_id, "chat": {"id": -100}}
    f.set_handler("sendMessage", on_send)

    await asyncio.wait_for(session.run(), timeout=20)
    # Session should exit cleanly after Claude's second result.
    assert session.exit_code == 0


async def test_heartbeat_updates_topic_name(fake_tg, tmp_path):
    f, base = fake_tg
    stub_path = tmp_path / "stub.py"
    stub_path.write_text(
        "import json, sys, time\n"
        "sys.stdout.write(json.dumps({'type':'result'})+'\\n'); sys.stdout.flush()\n"
        "time.sleep(2.0)\n"
    )
    cfg = Config(
        telegram_bot_token="TOKEN",
        telegram_supergroup_id=-100,
        allowed_user_ids=[42],
        heartbeat_interval_sec=0,  # fire every tick (clamped to 1s)
    )
    session = Session(
        config=cfg,
        claude_argv=[sys.executable, str(stub_path)],
        telegram_base_url=base,
        socket_path=str(tmp_path / "s.sock"),
        initial_prompt="go",
    )
    await asyncio.wait_for(session.run(), timeout=15)
    assert any(c[0] == "editForumTopic" for c in f.calls)


async def test_deny_tell_flow(fake_tg, tmp_path):
    """User taps Deny+tell, types a reason, which is sent back to Claude as the denial reason."""
    f, base = fake_tg
    stub_path = tmp_path / "stub.py"
    stub_path.write_text(STUB_CLAUDE)

    cfg = Config(
        telegram_bot_token="TOKEN",
        telegram_supergroup_id=-100,
        allowed_user_ids=[42],
    )
    session = Session(
        config=cfg,
        claude_argv=[sys.executable, str(stub_path)],
        telegram_base_url=base,
        socket_path=str(tmp_path / "s.sock"),
        initial_prompt="go",
    )

    async def on_send(body):
        if "reply_markup" in body:
            f.next_message_id += 1
            mid = f.next_message_id

            async def push():
                await asyncio.sleep(0.05)
                f.push_update({
                    "update_id": 1,
                    "callback_query": {
                        "id": "cbq1", "from": {"id": 42},
                        "data": f"{mid}:deny_tell",
                        "message": {"message_id": mid, "chat": {"id": -100}},
                    },
                })
                await asyncio.sleep(0.1)
                f.push_update({
                    "update_id": 2,
                    "message": {"message_id": 90, "from": {"id": 42},
                                "chat": {"id": -100},
                                "message_thread_id": 51,
                                "text": "too destructive"},
                })
                await asyncio.sleep(0.2)
                f.push_update({
                    "update_id": 3,
                    "message": {"message_id": 99, "from": {"id": 42},
                                "chat": {"id": -100},
                                "message_thread_id": 51,
                                "text": "continue"},
                })
            asyncio.create_task(push())
            return {"message_id": mid, "chat": {"id": -100}}
        f.next_message_id += 1
        return {"message_id": f.next_message_id, "chat": {"id": -100}}
    f.set_handler("sendMessage", on_send)

    await asyncio.wait_for(session.run(), timeout=20)
    assert session.exit_code == 0
    # Verify editMessageText was called (the "send a reason" prompt)
    assert any(c[0] == "editMessageText" for c in f.calls)
