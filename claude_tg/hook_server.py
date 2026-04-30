from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("claude_tg.hook_server")


@dataclass
class PreToolUseRequest:
    tool_name: str
    tool_input: dict
    _future: asyncio.Future = field(default=None, init=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._future = asyncio.get_running_loop().create_future()

    def resolve(self, *, decision: str, reason: str | None = None) -> None:
        if self._future.done():
            return
        out: dict[str, Any] = {"decision": decision}
        if reason is not None:
            out["reason"] = reason
        self._future.set_result(out)

    async def wait(self) -> dict:
        return await self._future


@dataclass
class StopRequest:
    last_assistant_text: str
    _future: asyncio.Future = field(default=None, init=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._future = asyncio.get_running_loop().create_future()

    def resolve(self, *, user_reply: str) -> None:
        if self._future.done():
            return
        self._future.set_result({"user_reply": user_reply})

    async def wait(self) -> dict:
        return await self._future


class HookServer:
    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._pre_tool_use: asyncio.Queue[PreToolUseRequest] = asyncio.Queue()
        self._stop: asyncio.Queue[StopRequest] = asyncio.Queue()

    async def start(self) -> None:
        if os.path.exists(self._path):
            os.unlink(self._path)
        self._server = await asyncio.start_unix_server(self._handle, path=self._path)
        os.chmod(self._path, 0o600)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(self._path):
            os.unlink(self._path)

    async def next_pre_tool_use(self) -> PreToolUseRequest:
        return await self._pre_tool_use.get()

    async def next_stop(self) -> StopRequest:
        return await self._stop.get()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
            endpoint = msg.get("endpoint")
            payload = msg.get("payload", {})

            if endpoint == "pre_tool_use":
                req = PreToolUseRequest(
                    tool_name=payload.get("tool_name", ""),
                    tool_input=payload.get("tool_input", {}),
                )
                await self._pre_tool_use.put(req)
                result = await req.wait()
            elif endpoint == "stop":
                req2 = StopRequest(last_assistant_text=payload.get("last_assistant_text", ""))
                await self._stop.put(req2)
                result = await req2.wait()
            else:
                result = {"error": f"unknown endpoint {endpoint}"}

            writer.write((json.dumps(result) + "\n").encode())
            await writer.drain()
        except Exception as e:
            log.exception("hook server error: %s", e)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
