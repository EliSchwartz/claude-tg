#!/bin/bash
# Discover your Telegram user id and the supergroup id for the bot.
#
# Usage:
#   1. In Telegram, send /start (or any message) to @bv_cc_bot directly — this captures your user id.
#   2. Create a supergroup, enable Topics, add the bot as admin with Manage Topics.
#   3. In the supergroup, post any message (e.g. "hi").
#   4. Run this script. It prints the most recent updates so you can copy the ids.

set -eu
TOKEN=$(awk -F'"' '/telegram_bot_token/ {print $2}' ~/.config/claude-tg/config.toml)
if [ -z "$TOKEN" ]; then
  echo "No token found in ~/.config/claude-tg/config.toml" >&2
  exit 1
fi

echo "== recent updates =="
curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates" | python3 -c '
import json, sys
data = json.load(sys.stdin)
if not data.get("ok"):
    print("ERROR:", data, file=sys.stderr); sys.exit(2)
users = set(); groups = set()
for u in data.get("result", []):
    msg = u.get("message") or u.get("edited_message") or {}
    chat = msg.get("chat", {})
    frm = msg.get("from", {})
    if frm.get("id"):
        users.add((frm["id"], frm.get("username") or frm.get("first_name", "?")))
    if chat.get("type") in ("group", "supergroup"):
        groups.add((chat["id"], chat.get("title", "?")))
    elif chat.get("type") == "private":
        users.add((chat["id"], chat.get("username") or chat.get("first_name", "?")))
if users:
    print("\n-- users seen --")
    for uid, name in sorted(users):
        print(f"  user id {uid}  ({name})")
else:
    print("\n(no private users seen — send the bot /start in a DM)")
if groups:
    print("\n-- groups seen --")
    for gid, title in sorted(groups):
        print(f"  group id {gid}  ({title})")
else:
    print("\n(no groups seen — create the supergroup, add the bot, then post a message)")
'
