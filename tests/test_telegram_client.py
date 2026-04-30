import pytest

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
        from aiohttp import web
        raise web.HTTPForbidden()
    f.set_handler("setMessageReaction", fail)
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    # Must not raise.
    await client.react(message_id=1, emoji="✅")


async def test_set_topic_name_degrades_on_failure(fake):
    f, base = fake
    async def fail(_body):
        from aiohttp import web
        raise web.HTTPForbidden()
    f.set_handler("editForumTopic", fail)
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    await client.set_topic_name(topic_id=50, name="session-xyz 🟢")


async def test_create_topic_fails_fatally(fake):
    f, base = fake
    async def fail(_body):
        from aiohttp import web
        raise web.HTTPForbidden()
    f.set_handler("createForumTopic", fail)
    client = TelegramClient(token="TOKEN", supergroup_id=-100, base_url=base)
    with pytest.raises(Exception):
        await client.create_topic("x")
