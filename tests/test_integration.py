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
    # The approval card should have been edited to show the decision, with
    # the inline keyboard stripped so buttons disappear.
    decision_edits = [
        body for method, body in f.calls
        if method == "editMessageText" and "Approved" in body.get("text", "")
    ]
    assert decision_edits, (
        f"expected editMessageText with 'Approved' on the approval card; "
        f"got calls: {[(m, b.get('text', '')[:40]) for m, b in f.calls if m == 'editMessageText']}"
    )
    # Buttons must be removed on the same edit.
    assert decision_edits[0].get("reply_markup") == {"inline_keyboard": []}
    # The edit should preserve the original tool preview, not lose context.
    assert "Approve tool: Bash" in decision_edits[0]["text"]


async def test_deny_edits_card_to_show_decision(fake_tg, tmp_path):
    """Tapping Deny should edit the approval card to show '❌ Denied' and
    strip the inline keyboard."""
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
        f.next_message_id += 1
        mid = f.next_message_id
        if "reply_markup" in body:
            async def push():
                await asyncio.sleep(0.05)
                f.push_update({
                    "update_id": 1,
                    "callback_query": {
                        "id": "cbq1", "from": {"id": 42},
                        "data": f"{mid}:deny",
                        "message": {"message_id": mid, "chat": {"id": -100}},
                    },
                })
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
    f.set_handler("sendMessage", on_send)

    await asyncio.wait_for(session.run(), timeout=20)
    assert session.exit_code == 0
    denied_edits = [
        body for method, body in f.calls
        if method == "editMessageText"
        and "Denied" in body.get("text", "")
    ]
    assert denied_edits, (
        f"expected 'Denied' edit on approval card; got: "
        f"{[(m, b.get('text', '')[:40]) for m, b in f.calls if m == 'editMessageText']}"
    )
    assert denied_edits[0].get("reply_markup") == {"inline_keyboard": []}


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
    # Two edits expected on the approval card:
    # 1. After deny_tell tap: prompt the user to send a reason (keyboard kept).
    # 2. After the reason text arrives: final decision card with the reason
    #    and keyboard stripped.
    edits = [body for method, body in f.calls if method == "editMessageText"]
    reason_edits = [b for b in edits if "too destructive" in b.get("text", "")]
    assert reason_edits, (
        f"expected final 'Denied: ...' edit to include the reason; got: "
        f"{[b.get('text', '')[:60] for b in edits]}"
    )
    assert reason_edits[0].get("reply_markup") == {"inline_keyboard": []}


async def test_stuck_approval_recovers_on_turn_end(fake_tg, tmp_path):
    """Regression: when the hook stub times out (human never tapped), Claude
    proceeds and emits TurnEnd. The wrapper must unstick its state so the
    next user text is forwarded to Claude instead of rejected with
    "approval is pending"."""
    f, base = fake_tg
    stub_path = tmp_path / "stub.py"
    # Stub Claude: emit tool_use, invoke the hook (which will time out since
    # we never tap any button), then emit result to end the turn. On next
    # user message, emit result again and exit.
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

    # Force the hook to time out quickly so the stub's hook-invoke returns
    # a deny and then the stub emits TurnEnd without the user ever tapping.
    env_patch_key = "CLAUDE_TG_HOOK_READ_TIMEOUT"
    prior = os.environ.get(env_patch_key)
    os.environ[env_patch_key] = "0.2"
    try:
        # When the approval card is posted, we DON'T tap anything. We just
        # wait for the hook to time out, TurnEnd to fire, and then send a
        # user text that should be accepted (forwarded to Claude).
        async def on_send(body):
            f.next_message_id += 1
            mid = f.next_message_id
            if "reply_markup" in body:
                async def push_reply_after_turn_end():
                    # Wait longer than the hook read timeout so Claude's stub
                    # gives up on the hook and emits result (TurnEnd).
                    await asyncio.sleep(0.6)
                    f.push_update({
                        "update_id": 1,
                        "message": {"message_id": 77, "from": {"id": 42},
                                    "chat": {"id": -100},
                                    "message_thread_id": 51,
                                    "text": "continue"},
                    })
                asyncio.create_task(push_reply_after_turn_end())
            return {"message_id": mid, "chat": {"id": -100}}
        f.set_handler("sendMessage", on_send)

        await asyncio.wait_for(session.run(), timeout=20)
    finally:
        if prior is None:
            os.environ.pop(env_patch_key, None)
        else:
            os.environ[env_patch_key] = prior

    # Session ran to completion: Claude got our "continue" message and exited.
    assert session.exit_code == 0
    # Crucially: no "approval is pending" reject was sent back to the user.
    rejects = [
        c for c in f.calls
        if c[0] == "sendMessage"
        and "approval is pending" in c[1].get("text", "")
    ]
    assert rejects == [], f"unexpected approval-pending reject: {rejects}"
    # The orchestrator must have cleared _current_approval when the turn
    # ended without a decision; otherwise a future tool_use would hit the
    # "overlapping approvals not supported" assertion.
    assert session._current_approval is None
    # And the user should have been told what happened, not left wondering.
    notes = [
        c for c in f.calls
        if c[0] == "sendMessage"
        and "timed out" in c[1].get("text", "").lower()
    ]
    assert notes, f"expected a timeout note to the user; got calls: {[c[0] for c in f.calls]}"


async def test_cli_exits_with_config_error_for_missing_config(tmp_path):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "claude_tg", "hi",
        "--config", str(tmp_path / "missing.toml"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode == 2
    assert b"config" in stderr.lower()
