#!/bin/bash
# Manual smoke test against a real Telegram bot and supergroup.
# Run after configuring ~/.config/claude-tg/config.toml.

set -eu
cd "$(dirname "$0")/.."

echo "== running protocol probe =="
python -m claude_tg.probe

echo
echo "== launching claude-tg with trivial prompt =="
echo "Check your Telegram supergroup for:"
echo "  1. new topic appears"
echo "  2. 🟢 session started message"
echo "  3. approval card for Bash tool"
echo "  4. tap Approve; see ✅ react on callback"
echo "  5. session exits with 🛑 marker"
echo
claude-tg "Run the bash command 'echo hello from claude-tg' and then stop."
