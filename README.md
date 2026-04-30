# claude-tg

A wrapper for Claude Code that relays tool-approval prompts and turn-end
questions to a Telegram forum topic, so you can drive sessions from your
phone.

## Setup

1. Create a Telegram bot with BotFather; save the token.
2. Create a supergroup, enable Topics, add the bot as an admin with
   "Manage Topics" permission.
3. Write `~/.config/claude-tg/config.toml`:

       telegram_bot_token = "123456:ABC..."
       telegram_supergroup_id = -1001234567890
       allowed_user_ids = [12345678]

4. `pip install -e .`
5. Run the protocol probe to verify your `claude` binary works with the
   wrapper: `python -m claude_tg.probe`. Fix any FAIL before proceeding.
6. `claude-tg "your first prompt"`. A new topic appears in the supergroup.

## Commands in the Telegram topic

- Tap Approve / Deny / Deny+tell on approval cards.
- On Deny+tell, send the reason as your next message.
- Type any message while the turn has ended to send it as the next prompt.
- `/cancel` — cancel a pending approval.
- `/stop` — gracefully end the session.

## Known limits

- The wrapper must stay running for the session's lifetime. Closing the
  terminal ends the session.
- Approvals and clarifications go to Telegram; bot rate limits apply.
- See `docs/superpowers/specs/2026-04-30-claude-tg-remote-control-design.md`
  for the full design and its security caveats.
