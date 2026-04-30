from claude_tg.session_state import (
    SessionState, State, ApprovalPending, ReplyPending, DenyReasonPending,
    ResolveApproval, ResolveReply, ResolveDenyReason, Reject, Ignore,
)


def test_initial_state_is_running():
    s = SessionState()
    assert s.state == State.RUNNING


def test_running_text_rejected():
    s = SessionState()
    action = s.on_text(user_id=1, text="hi")
    assert isinstance(action, Reject)
    assert "working" in action.message.lower() or "wait" in action.message.lower()


def test_turn_end_moves_to_waiting_user_reply():
    s = SessionState()
    s.on_turn_end()
    assert s.state == State.WAITING_USER_REPLY


def test_user_reply_resolves_and_goes_back_to_running():
    s = SessionState()
    s.on_turn_end()
    action = s.on_text(user_id=1, text="yes do Y")
    assert isinstance(action, ResolveReply)
    assert action.text == "yes do Y"
    assert s.state == State.RUNNING


def test_pre_tool_use_moves_to_waiting_approval():
    s = SessionState()
    s.on_pre_tool_use(approval_message_id=100)
    assert s.state == State.WAITING_TOOL_APPROVAL
    assert isinstance(s.pending, ApprovalPending)
    assert s.pending.approval_message_id == 100


def test_text_while_waiting_approval_is_rejected():
    s = SessionState()
    s.on_pre_tool_use(approval_message_id=100)
    action = s.on_text(user_id=1, text="hey")
    assert isinstance(action, Reject)
    assert "approval" in action.message.lower()


def test_approve_callback_resolves_and_goes_to_running():
    s = SessionState()
    s.on_pre_tool_use(approval_message_id=100)
    action = s.on_callback(approval_message_id=100, kind="approve")
    assert isinstance(action, ResolveApproval)
    assert action.decision == "approve"
    assert s.state == State.RUNNING


def test_deny_tell_moves_to_deny_reason_state():
    s = SessionState()
    s.on_pre_tool_use(approval_message_id=100)
    action = s.on_callback(approval_message_id=100, kind="deny_tell")
    assert isinstance(action, Ignore)  # wrapper edits message; no immediate resolve
    assert s.state == State.WAITING_DENY_REASON


def test_text_in_deny_reason_state_resolves_with_reason():
    s = SessionState()
    s.on_pre_tool_use(approval_message_id=100)
    s.on_callback(approval_message_id=100, kind="deny_tell")
    action = s.on_text(user_id=1, text="that was destructive")
    assert isinstance(action, ResolveDenyReason)
    assert action.reason == "that was destructive"
    assert s.state == State.RUNNING


def test_stale_callback_ignored():
    s = SessionState()
    s.on_pre_tool_use(approval_message_id=100)
    action = s.on_callback(approval_message_id=999, kind="approve")
    assert isinstance(action, Ignore)
    assert s.state == State.WAITING_TOOL_APPROVAL


def test_cancel_in_waiting_approval_denies():
    s = SessionState()
    s.on_pre_tool_use(approval_message_id=100)
    action = s.on_cancel()
    assert isinstance(action, ResolveApproval)
    assert action.decision == "deny"
    assert "cancel" in (action.reason or "").lower()
    assert s.state == State.RUNNING


def test_cancel_in_running_state_is_noop():
    s = SessionState()
    action = s.on_cancel()
    assert isinstance(action, Reject)
    assert "nothing" in action.message.lower()


def test_ended_ignores_everything():
    s = SessionState()
    s.on_end()
    assert s.state == State.ENDED
    assert isinstance(s.on_text(1, "hi"), Ignore)
    assert isinstance(s.on_callback(100, "approve"), Ignore)
