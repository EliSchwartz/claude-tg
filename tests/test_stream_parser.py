from claude_tg.stream_parser import (
    AssistantText, ToolUse, TurnEnd, SessionEnd, parse_events,
)


def test_parses_assistant_text_event():
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
    events = list(parse_events([line]))
    assert events == [AssistantText(text="hi")]


def test_parses_tool_use_event():
    line = (
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls"}}]}}\n'
    )
    events = list(parse_events([line]))
    assert events == [ToolUse(tool_id="t1", name="Bash", input={"command": "ls"})]


def test_parses_turn_end_event():
    line = '{"type":"result"}\n'
    events = list(parse_events([line]))
    assert events == [TurnEnd()]


def test_ignores_unknown_event_types():
    line = '{"type":"system","subtype":"init"}\n'
    assert list(parse_events([line])) == []


def test_ignores_invalid_json():
    assert list(parse_events(["{not json}\n", "\n"])) == []


def test_handles_multiple_content_blocks_in_one_assistant_event():
    line = (
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"before"},'
        '{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls"}},'
        '{"type":"text","text":"after"}'
        ']}}\n'
    )
    events = list(parse_events([line]))
    assert events == [
        AssistantText(text="before"),
        ToolUse(tool_id="t1", name="Bash", input={"command": "ls"}),
        AssistantText(text="after"),
    ]
