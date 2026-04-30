from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Union


class State(Enum):
    RUNNING = "running"
    WAITING_TOOL_APPROVAL = "waiting_tool_approval"
    WAITING_DENY_REASON = "waiting_deny_reason"
    WAITING_USER_REPLY = "waiting_user_reply"
    ENDED = "ended"


@dataclass(frozen=True)
class ApprovalPending:
    approval_message_id: int


@dataclass(frozen=True)
class ReplyPending:
    pass


@dataclass(frozen=True)
class DenyReasonPending:
    approval_message_id: int


Pending = Union[ApprovalPending, ReplyPending, DenyReasonPending, None]


# Actions are value objects describing what the orchestrator should do.
@dataclass(frozen=True)
class ResolveApproval:
    decision: str       # "approve" | "deny"
    reason: str | None


@dataclass(frozen=True)
class ResolveDenyReason:
    reason: str


@dataclass(frozen=True)
class ResolveReply:
    text: str


@dataclass(frozen=True)
class Reject:
    message: str


@dataclass(frozen=True)
class Ignore:
    pass


Action = Union[ResolveApproval, ResolveDenyReason, ResolveReply, Reject, Ignore]


class SessionState:
    def __init__(self) -> None:
        self.state: State = State.RUNNING
        self.pending: Pending = None

    # Internal signals
    def on_pre_tool_use(self, approval_message_id: int) -> None:
        self.state = State.WAITING_TOOL_APPROVAL
        self.pending = ApprovalPending(approval_message_id=approval_message_id)

    def on_turn_end(self) -> None:
        if self.state == State.RUNNING:
            self.state = State.WAITING_USER_REPLY
            self.pending = ReplyPending()

    def on_end(self) -> None:
        self.state = State.ENDED
        self.pending = None

    # Telegram-side events
    def on_text(self, user_id: int, text: str) -> Action:
        if self.state == State.WAITING_USER_REPLY:
            self.state = State.RUNNING
            self.pending = None
            return ResolveReply(text=text)
        if self.state == State.WAITING_DENY_REASON:
            # clear pending after caller resolves the original approval request
            self.state = State.RUNNING
            self.pending = None
            return ResolveDenyReason(reason=text)
        if self.state == State.WAITING_TOOL_APPROVAL:
            return Reject(message="⏸ an approval is pending — respond to it first")
        if self.state == State.RUNNING:
            return Reject(message="⚠ session is working — wait for the next turn or use /cancel")
        return Ignore()  # ENDED

    def on_callback(self, approval_message_id: int, kind: str) -> Action:
        if self.state == State.WAITING_TOOL_APPROVAL and isinstance(self.pending, ApprovalPending):
            if self.pending.approval_message_id != approval_message_id:
                return Ignore()
            if kind == "approve":
                self.state = State.RUNNING
                self.pending = None
                return ResolveApproval(decision="approve", reason=None)
            if kind == "deny":
                self.state = State.RUNNING
                self.pending = None
                return ResolveApproval(decision="deny", reason="denied by user")
            if kind == "deny_tell":
                self.state = State.WAITING_DENY_REASON
                self.pending = DenyReasonPending(
                    approval_message_id=approval_message_id,
                )
                return Ignore()
        return Ignore()

    def on_cancel(self) -> Action:
        if self.state == State.WAITING_TOOL_APPROVAL:
            self.state = State.RUNNING
            self.pending = None
            return ResolveApproval(decision="deny", reason="cancelled by user")
        if self.state == State.WAITING_DENY_REASON:
            self.state = State.RUNNING
            self.pending = None
            return ResolveApproval(decision="deny", reason="denied by user")
        return Reject(message="nothing to cancel; use /stop to end the session")
