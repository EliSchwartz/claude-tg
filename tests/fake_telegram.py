"""In-process fake Telegram Bot API server for tests.

Listens on localhost and records calls. Tests point the real client at
this server by overriding the base URL.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiohttp import web


@dataclass
class FakeTelegram:
    calls: list[tuple[str, dict]] = field(default_factory=list)
    update_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    next_message_id: int = 1000
    next_topic_id: int = 50
    handlers: dict[str, Callable[[dict], Awaitable[Any]]] = field(default_factory=dict)
    runner: web.AppRunner | None = None
    site: web.TCPSite | None = None
    port: int = 0

    def set_handler(self, method: str, fn: Callable[[dict], Awaitable[Any]]) -> None:
        self.handlers[method] = fn

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("POST", "/bot{token}/{method}", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sock = list(self.site._server.sockets)[0]
        self.port = sock.getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def _handle(self, req: web.Request) -> web.Response:
        method = req.match_info["method"]
        body = await req.json() if req.body_exists else {}
        self.calls.append((method, body))

        if method in self.handlers:
            result = await self.handlers[method](body)
            return web.json_response({"ok": True, "result": result})

        if method == "createForumTopic":
            self.next_topic_id += 1
            return web.json_response({"ok": True, "result": {
                "message_thread_id": self.next_topic_id,
                "name": body.get("name", ""),
            }})

        if method == "sendMessage":
            self.next_message_id += 1
            return web.json_response({"ok": True, "result": {
                "message_id": self.next_message_id,
                "chat": {"id": body.get("chat_id")},
                "text": body.get("text", ""),
            }})

        if method == "setMessageReaction":
            return web.json_response({"ok": True, "result": True})

        if method == "editMessageReplyMarkup":
            return web.json_response({"ok": True, "result": True})

        if method == "editMessageText":
            return web.json_response({"ok": True, "result": True})

        if method == "answerCallbackQuery":
            return web.json_response({"ok": True, "result": True})

        if method == "editForumTopic":
            return web.json_response({"ok": True, "result": True})

        if method == "closeForumTopic":
            return web.json_response({"ok": True, "result": True})

        if method == "getUpdates":
            # Drain any queued updates without blocking for long; tests push
            # updates ahead of time.
            results = []
            while not self.update_queue.empty():
                results.append(await self.update_queue.get())
            return web.json_response({"ok": True, "result": results})

        return web.json_response({"ok": False, "description": f"unknown method {method}"})

    def push_update(self, update: dict) -> None:
        self.update_queue.put_nowait(update)
