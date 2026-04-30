import pytest
from aiohttp import web

from claude_tg.telegram_client import TelegramClient
from tests.fake_telegram import FakeTelegram


@pytest.fixture
async def fake():
    f = FakeTelegram()
    base = await f.start()
    try:
        yield f, base
    finally:
        await f.stop()


async def test_create_topic(fake):
    f, base = fake
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    topic_id = await client.create_topic("session-abc")
    assert isinstance(topic_id, int)
    assert f.calls[0][0] == "createForumTopic"
    assert f.calls[0][1]["chat_id"] == -100
    assert f.calls[0][1]["name"] == "session-abc"


async def test_post_message_chunks_long_text(fake):
    f, base = fake
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    long = "x" * 9000
    msg_ids = await client.post_message(topic_id=50, text=long)
    assert len(msg_ids) == 3  # 9000 / 4000 = 2.25 -> 3 chunks
    assert all(call[0] == "sendMessage" for call in f.calls)


async def test_react_degrades_on_failure(fake):
    f, base = fake
    async def fail(_body):
        raise web.HTTPForbidden()
    f.set_handler("setMessageReaction", fail)
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    # Must not raise.
    await client.react(message_id=1, emoji="✅")


async def test_set_topic_name_degrades_on_failure(fake):
    f, base = fake
    async def fail(_body):
        raise web.HTTPForbidden()
    f.set_handler("editForumTopic", fail)
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    await client.set_topic_name(topic_id=50, name="session-xyz 🟢")


async def test_create_topic_fails_fatally(fake):
    f, base = fake
    async def fail(_body):
        raise web.HTTPForbidden()
    f.set_handler("createForumTopic", fail)
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    with pytest.raises(Exception):
        await client.create_topic("x")


async def test_post_message_empty_string_is_noop(fake):
    f, base = fake
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    result = await client.post_message(topic_id=50, text="")
    assert result == []
    assert f.calls == []


async def test_post_approval_has_callback_data_with_expected_suffixes(fake):
    f, base = fake
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    mid = await client.post_approval(
        topic_id=50, tool_name="Bash", preview="ls",
    )
    # First call: sendMessage with placeholder keyboard.
    assert f.calls[0][0] == "sendMessage"
    # Second call: editMessageReplyMarkup with real callback_data keyed by mid.
    assert f.calls[1][0] == "editMessageReplyMarkup"
    assert f.calls[1][1]["message_id"] == mid
    keyboard = f.calls[1][1]["reply_markup"]["inline_keyboard"][0]
    datas = [btn["callback_data"] for btn in keyboard]
    assert f"{mid}:approve" in datas
    assert f"{mid}:deny" in datas
    assert f"{mid}:deny_tell" in datas


async def test_post_approval_handles_backticks_in_preview(fake):
    f, base = fake
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    # preview with triple backticks - must NOT use parse_mode=Markdown
    await client.post_approval(
        topic_id=50, tool_name="Edit",
        preview="```python\ncode\n```",
    )
    # ensure parse_mode is not set (plain text)
    assert "parse_mode" not in f.calls[0][1]


async def test_poll_updates_yields_text_and_callbacks(fake):
    f, base = fake
    # Push two updates: one text message, one callback query.
    f.push_update({
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": 42},
            "chat": {"id": -100},
            "message_thread_id": 50,
            "text": "hello",
        },
    })
    f.push_update({
        "update_id": 2,
        "callback_query": {
            "id": "cbq1",
            "from": {"id": 42},
            "data": "pref:approve",
            "message": {"message_id": 20, "chat": {"id": -100}},
        },
    })
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)

    got = []
    async for event in client.poll_updates(stop_after=2):
        got.append(event)

    from claude_tg.telegram_client import TextUpdate, CallbackUpdate
    assert got[0] == TextUpdate(
        from_user_id=42, topic_id=50, message_id=10, text="hello",
    )
    assert got[1] == CallbackUpdate(
        from_user_id=42, message_id=20, data="pref:approve", callback_query_id="cbq1",
    )


async def test_poll_updates_recovers_from_transient_getupdates_failure(fake):
    f, base = fake
    call_count = {"n": 0}
    async def flaky(_body):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("transient")
        # Second call: return a real update.
        return [{
            "update_id": 1,
            "message": {
                "message_id": 10, "from": {"id": 42},
                "chat": {"id": -100}, "message_thread_id": 50,
                "text": "after blip",
            },
        }]
    f.set_handler("getUpdates", flaky)
    # Override the client's default retry/backoff to speed up the test
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)

    from claude_tg.telegram_client import TextUpdate
    got = []
    async for event in client.poll_updates(stop_after=1):
        got.append(event)
    assert isinstance(got[0], TextUpdate)
    assert got[0].text == "after blip"
