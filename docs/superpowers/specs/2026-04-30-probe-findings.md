# claude-tg protocol probe findings

**Date:** 2026-04-30
**Claude Code version:** 2.1.123 (Claude Code)
**Python:** 3.10.14
**Endpoint:** LiteLLM-proxied (see `~/.claude/settings.json`), `ANTHROPIC_BASE_URL=https://ete-litellm.ai-models.vpc-int.res.ibm.com`, `apiKeySource: "none"` in system/init event (auth via `ANTHROPIC_AUTH_TOKEN` env).

## Results

| Check | Result | Notes |
|-------|--------|-------|
| 1. Multi-turn stdin | PASS | Both user messages produced distinct `assistant` events in the same process. |
| 2. End-of-turn event | PASS | exact event type = `"result"`. Full sequence observed: `['system', 'system', 'system', 'assistant', 'result']`. |
| 3. `--settings` overrides | PASS | Temp `--settings` file registered a `PreToolUse` hook and the hook fired when Claude ran Bash. User-level settings did not prevent it. |
| 4. Hook payload shape | PASS | See appendix. Shape: top-level `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`. Hook approve-reply used was `{"decision": "approve"}` (stdout); claude accepted it and completed the tool call. |

## Reference constants (for downstream tasks)

From the observed stream, later tasks should use these exact strings:

- **End-of-turn event**: `type == "result"` (with `subtype == "success"` on normal completion, and a top-level `result` field carrying the final assistant text).
- **Assistant event**: `type == "assistant"`, with nested `message.content` list.
- **System init event**: `type == "system"`, `subtype == "init"` (carries `session_id`, `model`, `tools`, `cwd`, `permissionMode`, `claude_code_version`).
- **System hook events** (new in 2.1.x): `type == "system"`, `subtype == "hook_started"` / `"hook_response"`.
- **PreToolUse hook stdin keys**: `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name` (= `"PreToolUse"`), `tool_name`, `tool_input`, `tool_use_id`.

## Deviation from plan: required CLI flags

The probe source in the plan invoked `claude` with `--input-format stream-json --output-format stream-json --print`. In Claude Code 2.1.123 this errors out with:

```
Error: When using --print, --output-format=stream-json requires --verbose
```

The probe was amended to add `--verbose`, at which point all four checks passed. Downstream tasks (Task 8 session orchestrator) must spawn `claude` with `--verbose --print --input-format stream-json --output-format stream-json` — the spec/plan should be updated to reflect this.

## Appendix: exact hook input JSON

Raw stdin delivered to a `PreToolUse` hook when Claude was asked to run `echo probe` via Bash:

```json
{
  "session_id": "32f80a7d-5962-40a0-b4c6-3570913b3f5e",
  "transcript_path": "/u/elisch/.claude/projects/-proj-mmfm-users-eli-repos-cc-remote/32f80a7d-5962-40a0-b4c6-3570913b3f5e.jsonl",
  "cwd": "/proj/mmfm/users/eli/repos/cc_remote",
  "permission_mode": "default",
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {
    "command": "echo probe",
    "description": "Echo probe"
  },
  "tool_use_id": "toolu_bdrk_01B9vKe3jxp15Xw9ATarUm2A"
}
```

The hook responded with `{"decision": "approve"}` on stdout and the tool proceeded.

## Appendix: exact stream-json assistant / result events

Observed from a minimal `say ok` session (whitespace reformatted for readability; real events are single-line):

Assistant event:

```json
{
  "type": "assistant",
  "message": {
    "model": "claude-opus-4-7",
    "id": "msg_bdrk_e2k6q5mhwo7aw4atxo6tyyog7yewqazomaibykd4fw7gxoixxo6q",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": null,
    "stop_sequence": null,
    "stop_details": null,
    "usage": {
      "input_tokens": 6,
      "cache_creation_input_tokens": 2442,
      "cache_read_input_tokens": 40960,
      "cache_creation": {
        "ephemeral_5m_input_tokens": 2442,
        "ephemeral_1h_input_tokens": 0
      },
      "output_tokens": 1,
      "service_tier": "standard"
    },
    "context_management": null
  },
  "parent_tool_use_id": null,
  "session_id": "8908d13d-a2a9-4558-aa8c-ac344fe335fa",
  "uuid": "a4eee2c2-70ff-4392-92e6-b794d14f3dca"
}
```

Result (end-of-turn) event:

```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "api_error_status": null,
  "duration_ms": 1679,
  "duration_api_ms": 1621,
  "num_turns": 1,
  "result": "ok",
  "stop_reason": "end_turn",
  "session_id": "8908d13d-a2a9-4558-aa8c-ac344fe335fa",
  "total_cost_usd": 0.0359225,
  "usage": { "...": "..." },
  "modelUsage": { "claude-opus-4-7[1m]": { "...": "..." } },
  "permission_denials": [],
  "terminal_reason": "completed",
  "fast_mode_state": "off",
  "uuid": "bbd26443-1ac5-462f-a544-ef48699a70f6"
}
```

Additional event types observed (not in the plan's assumption list, useful for Task 3 parser):

- `type: "system", subtype: "init"` — emitted once at startup; carries `session_id`, `model`, `claude_code_version`, `permissionMode`, tool list.
- `type: "system", subtype: "hook_started"` / `"hook_response"` — emitted for each hook invocation (including `SessionStart:startup`). In this environment the `superpowers` plugin fires a large `SessionStart` hook that injects the `using-superpowers` skill text as additional context. Task 3's parser should accept and ignore unknown `system` subtypes gracefully.

## Summary

All four Implementation Validation Checklist items PASS. The architecture decision (Stop-hook-blocking) in the spec is viable. The only required plan amendment is to add `--verbose` to the `claude` subprocess invocation everywhere stream-json I/O is used with `--print`.
