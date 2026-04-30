"""Protocol validation probe for claude-tg.

Validates the four Claude Code protocol assumptions before we build the
Telegram relay. Run via: python -m claude_tg.probe
"""

# Note: every `claude` invocation below passes `--verbose` because Claude Code
# 2.1.x requires `--verbose` when combining `--print` with
# `--output-format stream-json`.

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


CHECK_MARK = "PASS"
X_MARK = "FAIL"


async def probe_1_multi_turn_stdin(claude_bin: str) -> tuple[bool, str]:
    """Check 1: Can Claude accept multiple user messages on stdin in stream-json mode?"""
    # Launch Claude in stream-json i/o mode with no initial prompt arg.
    # Write a user message, wait for response, write a second user message,
    # wait for a second response.
    proc = await asyncio.create_subprocess_exec(
        claude_bin,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--print",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        def user_msg(text: str) -> bytes:
            return (json.dumps({
                "type": "user",
                "message": {"role": "user", "content": text},
            }) + "\n").encode()

        proc.stdin.write(user_msg("say the word banana and nothing else"))
        await proc.stdin.drain()

        # Track turn boundaries by `result` events. An `assistant` event must
        # appear between sending a user message and the corresponding `result`
        # for that turn to count as a real response.
        results_seen = 0
        assistant_since_last_result = False
        saw_first_response = False
        saw_second_response = False

        async def read_loop():
            nonlocal results_seen, assistant_since_last_result
            nonlocal saw_first_response, saw_second_response
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                if not line:
                    return
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "assistant":
                    assistant_since_last_result = True
                elif t == "result":
                    results_seen += 1
                    if results_seen == 1:
                        saw_first_response = assistant_since_last_result
                        assistant_since_last_result = False
                        # First turn complete; send the second user message.
                        proc.stdin.write(user_msg("now say apple and nothing else"))
                        await proc.stdin.drain()
                    elif results_seen == 2:
                        saw_second_response = assistant_since_last_result
                        return

        await asyncio.wait_for(read_loop(), timeout=120)
        ok = saw_first_response and saw_second_response
        return ok, ("multi-turn stdin works" if ok
                    else f"first={saw_first_response} second={saw_second_response}")
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def probe_2_end_of_turn_event(claude_bin: str) -> tuple[bool, str]:
    """Check 2: What event type marks end-of-turn? Return the set we observe."""
    proc = await asyncio.create_subprocess_exec(
        claude_bin,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--print",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "say ok"},
        }) + "\n"
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()

        seen_types: list[str] = []
        async def read_loop():
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                if not line:
                    return
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type", "?")
                seen_types.append(t)
                if t in ("result", "stop"):
                    return

        await asyncio.wait_for(read_loop(), timeout=90)
        if seen_types and seen_types[-1] in ("result", "stop"):
            return True, f"end-of-turn event type: {seen_types[-1]} (seen: {seen_types})"
        return False, f"no clear end-of-turn seen; events: {seen_types}"
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def probe_3_settings_precedence(claude_bin: str) -> tuple[bool, str]:
    """Check 3: Does --settings override user settings for hooks + permissions?

    We set a no-op PreToolUse hook in a temp settings file and observe whether
    Claude invokes it when asked to run a tool.
    """
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        marker = tdp / "hook_fired"
        # A hook script that just creates a marker file and returns approve.
        hook_py = tdp / "hook.py"
        hook_py.write_text(
            "import json, sys\n"
            f"open({str(marker)!r}, 'w').close()\n"
            "print(json.dumps({'decision': 'approve'}))\n"
        )
        settings_json = tdp / "settings.json"
        settings_json.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [{"type": "command",
                               "command": f"{sys.executable} {hook_py}"}],
                }],
            },
            "permissions": {"defaultMode": "default"},
        }))

        proc = await asyncio.create_subprocess_exec(
            claude_bin,
            "--settings", str(settings_json),
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--print",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            msg = json.dumps({
                "type": "user",
                "message": {"role": "user",
                            "content": "Run the bash command `echo hi` right now."},
            }) + "\n"
            proc.stdin.write(msg.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        fired = marker.exists()
        return fired, ("hook fired (settings override works)"
                       if fired else "hook did NOT fire — check settings precedence")


async def probe_4_hook_payload_shape(claude_bin: str) -> tuple[bool, str]:
    """Check 4: What JSON does PreToolUse send to stdin, and what does it expect back?

    We install a hook that dumps its stdin and writes a known approve response.
    We then read the dumped input and report its shape.
    """
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        dump = tdp / "hook_input.json"
        hook_py = tdp / "hook.py"
        hook_py.write_text(
            "import json, sys\n"
            "data = sys.stdin.read()\n"
            f"open({str(dump)!r}, 'w').write(data)\n"
            "print(json.dumps({'decision': 'approve'}))\n"
        )
        settings_json = tdp / "settings.json"
        settings_json.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [{"type": "command",
                               "command": f"{sys.executable} {hook_py}"}],
                }],
            },
            "permissions": {"defaultMode": "default"},
        }))

        proc = await asyncio.create_subprocess_exec(
            claude_bin,
            "--settings", str(settings_json),
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--print",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            msg = json.dumps({
                "type": "user",
                "message": {"role": "user",
                            "content": "Run the bash command `echo probe`."},
            }) + "\n"
            proc.stdin.write(msg.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        if not dump.exists():
            return False, "hook never fired"
        payload = dump.read_text()
        return True, f"hook payload shape observed ({len(payload)} bytes):\n{payload[:800]}"


async def main_async() -> int:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        print(f"{X_MARK} claude binary not found on PATH")
        return 2

    checks = [
        ("1. multi-turn stdin",     probe_1_multi_turn_stdin),
        ("2. end-of-turn event",    probe_2_end_of_turn_event),
        ("3. --settings overrides", probe_3_settings_precedence),
        ("4. hook payload shape",   probe_4_hook_payload_shape),
    ]
    all_ok = True
    for label, fn in checks:
        print(f"--- {label} ---")
        try:
            ok, detail = await fn(claude_bin)
        except Exception as e:
            ok, detail = False, f"exception: {e!r}"
        mark = CHECK_MARK if ok else X_MARK
        print(f"{mark} {label}: {detail}\n")
        all_ok = all_ok and ok
    return 0 if all_ok else 1


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
