import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from claude_tg.hook_server import HookServer, PreToolUseRequest


async def test_pre_tool_use_blocks_until_resolved(tmp_path):
    sock = str(tmp_path / "s.sock")
    server = HookServer(socket_path=sock)
    await server.start()
    try:
        # Start a "client" that sends a pre_tool_use request.
        async def client():
            reader, writer = await asyncio.open_unix_connection(sock)
            req = {"endpoint": "pre_tool_use",
                   "payload": {"tool_name": "Bash", "tool_input": {"command": "ls"}}}
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            return json.loads(line)

        client_task = asyncio.create_task(client())

        # Server should expose the pending request.
        req = await asyncio.wait_for(server.next_pre_tool_use(), timeout=2.0)
        assert isinstance(req, PreToolUseRequest)
        assert req.tool_name == "Bash"

        # Resolve it.
        req.resolve(decision="approve")

        result = await asyncio.wait_for(client_task, timeout=2.0)
        assert result == {"decision": "approve"}
    finally:
        await server.stop()


async def test_hook_script_defaults_deny_on_dead_socket(tmp_path):
    """If the wrapper is gone, the stub returns a deny so Claude doesn't hang."""
    # Run the hook_script against a non-existent socket.
    hook_script = Path(__file__).parent.parent / "claude_tg" / "hook_script.py"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(hook_script),
        str(tmp_path / "does_not_exist.sock"), "pre_tool_use",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate(b'{"tool_name":"Bash","tool_input":{}}')
    out = json.loads(stdout)
    assert out["decision"] == "deny"
