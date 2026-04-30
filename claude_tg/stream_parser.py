from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Iterator, Union


@dataclass(frozen=True)
class AssistantText:
    text: str


@dataclass(frozen=True)
class ToolUse:
    tool_id: str
    name: str
    input: dict


@dataclass(frozen=True)
class TurnEnd:
    pass


Event = Union[AssistantText, ToolUse, TurnEnd]


def parse_events(lines: Iterable[str]) -> Iterator[Event]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = ev.get("type")
        if t == "assistant":
            for block in ev.get("message", {}).get("content", []) or []:
                bt = block.get("type")
                if bt == "text":
                    yield AssistantText(text=block.get("text", ""))
                elif bt == "tool_use":
                    yield ToolUse(
                        tool_id=block.get("id", ""),
                        name=block.get("name", ""),
                        input=block.get("input", {}) or {},
                    )
        elif t == "result":
            yield TurnEnd()
        # unknown event types are silently skipped
