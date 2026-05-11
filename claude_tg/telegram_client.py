from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx


log = logging.getLogger("claude_tg.telegram")

MAX_CHUNK = 4000  # Telegram's hard limit is 4096; leave headroom.


@dataclass(frozen=True)
class TextUpdate:
    from_user_id: int
    topic_id: int | None
    message_id: int
    text: str


@dataclass(frozen=True)
class CallbackUpdate:
    from_user_id: int
    message_id: int
    data: str
    callback_query_id: str


class TelegramClient:
    def __init__(
        self,
        token: str,
        supergroup_id: int,
        base_url: str = "https://api.telegram.org",
        timeout: float = 30.0,
    ) -> None:
        self._token = token
        self._supergroup_id = supergroup_id
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, **params: Any) -> Any:
        url = f"{self._base}/{method}"
        last_err: Exception | None = None
        delay = 1.0
        for attempt in range(5):
            try:
                resp = await self._http.post(url, json=params)
                resp.raise_for_status()
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegram error: {data}")
                return data["result"]
            except httpx.HTTPStatusError as e:
                last_err = e
                if attempt == 4:
                    break
                retry_after = None
                try:
                    body = e.response.json()
                    retry_after = body.get("parameters", {}).get("retry_after")
                except Exception:
                    pass
                sleep_s = float(retry_after) if retry_after else min(delay, 30.0)
                await asyncio.sleep(sleep_s)
                if not retry_after:
                    delay *= 2
            except Exception as e:
                last_err = e
                if attempt == 4:
                    break
                await asyncio.sleep(min(delay, 30.0))
                delay *= 2
        assert last_err is not None
        raise last_err

    async def _call_soft(self, method: str, **params: Any) -> Any | None:
        """Call that logs-and-continues on failure; for non-critical methods."""
        try:
            return await self._call(method, **params)
        except Exception as e:
            log.warning("telegram %s failed (degraded): %s", method, e)
            return None

    async def create_topic(self, name: str) -> int:
        result = await self._call(
            "createForumTopic",
            chat_id=self._supergroup_id,
            name=name,
        )
        return int(result["message_thread_id"])

    async def post_message(self, topic_id: int, text: str) -> list[int]:
        if not text:
            return []
        ids: list[int] = []
        for i in range(0, len(text), MAX_CHUNK):
            chunk = text[i : i + MAX_CHUNK]
            result = await self._call(
                "sendMessage",
                chat_id=self._supergroup_id,
                message_thread_id=topic_id,
                text=chunk,
            )
            ids.append(int(result["message_id"]))
        return ids

    async def post_approval(
        self,
        topic_id: int,
        tool_name: str,
        preview: str,
    ) -> tuple[int, str]:
        """Post an approval card with an inline keyboard whose callback_data
        uses the returned message_id as the prefix. This guarantees callbacks
        can be routed back to the right pending request.

        Returns (message_id, card_text) so the caller can later edit the card
        to show the resolved decision.

        Text is sent as plain text (no parse_mode) because tool input previews
        may contain arbitrary characters including backticks.
        """
        # Post with placeholder buttons (no real callback_data yet); get the
        # real message_id.
        placeholder_kb = {"inline_keyboard": [[
            {"text": "⏳", "callback_data": "placeholder"},
        ]]}
        text = f"⚠️ Approve tool: {tool_name}\n\n{preview}"
        result = await self._call(
            "sendMessage",
            chat_id=self._supergroup_id,
            message_thread_id=topic_id,
            text=text,
            reply_markup=placeholder_kb,
        )
        message_id = int(result["message_id"])
        # Now set the real buttons with callback_data keyed on message_id.
        real_kb = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"{message_id}:approve"},
            {"text": "❌ Deny",    "callback_data": f"{message_id}:deny"},
            {"text": "✏️ Deny+tell","callback_data": f"{message_id}:deny_tell"},
        ]]}
        await self._call_soft(
            "editMessageReplyMarkup",
            chat_id=self._supergroup_id,
            message_id=message_id,
            reply_markup=real_kb,
        )
        return message_id, text

    async def react(self, message_id: int, emoji: str) -> None:
        await self._call_soft(
            "setMessageReaction",
            chat_id=self._supergroup_id,
            message_id=message_id,
            reaction=[{"type": "emoji", "emoji": emoji}],
        )

    async def edit_message_text(
        self,
        message_id: int,
        text: str,
        *,
        strip_keyboard: bool = False,
    ) -> None:
        kwargs: dict[str, Any] = {
            "chat_id": self._supergroup_id,
            "message_id": message_id,
            "text": text,
        }
        if strip_keyboard:
            # Passing an empty inline_keyboard removes the buttons in the
            # same RPC as the text edit, so the user never sees a stale
            # button briefly during the resolve.
            kwargs["reply_markup"] = {"inline_keyboard": []}
        await self._call_soft("editMessageText", **kwargs)

    async def set_topic_name(self, topic_id: int, name: str) -> None:
        await self._call_soft(
            "editForumTopic",
            chat_id=self._supergroup_id,
            message_thread_id=topic_id,
            name=name,
        )

    async def close_topic(self, topic_id: int) -> None:
        await self._call_soft(
            "closeForumTopic",
            chat_id=self._supergroup_id,
            message_thread_id=topic_id,
        )

    async def answer_callback(self, callback_query_id: str) -> None:
        await self._call_soft("answerCallbackQuery", callback_query_id=callback_query_id)

    async def poll_updates(self, stop_after: int | None = None):
        """Yield TextUpdate and CallbackUpdate events.

        On transient errors from Telegram (network blips, 5xx, auth), log and
        continue polling rather than terminating the generator — this keeps the
        session responsive to Telegram input across brief outages.

        stop_after is a test hook: stop after yielding N events.
        """
        offset: int | None = None
        yielded = 0
        while True:
            params: dict[str, Any] = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            try:
                updates = await self._call("getUpdates", **params)
            except Exception as e:
                log.warning("getUpdates failed; continuing: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates or []:
                try:
                    offset = int(u["update_id"]) + 1
                    if "message" in u:
                        m = u["message"]
                        if "text" not in m:
                            continue
                        yield TextUpdate(
                            from_user_id=int(m.get("from", {}).get("id", 0)),
                            topic_id=m.get("message_thread_id"),
                            message_id=int(m["message_id"]),
                            text=m["text"],
                        )
                        yielded += 1
                    elif "callback_query" in u:
                        cb = u["callback_query"]
                        yield CallbackUpdate(
                            from_user_id=int(cb.get("from", {}).get("id", 0)),
                            message_id=int(cb["message"]["message_id"]),
                            data=cb.get("data", ""),
                            callback_query_id=cb["id"],
                        )
                        yielded += 1
                    # edited_message and other update types are intentionally ignored
                except Exception as e:
                    log.warning("skipping malformed update: %s", e)
                    continue
                if stop_after is not None and yielded >= stop_after:
                    return
