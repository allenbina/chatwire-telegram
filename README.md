# chatwire-telegram

Telegram integration plugin for [chatwire](https://github.com/allenbina/chatwire) — the macOS iMessage relay bridge.

Renders inbound iMessage events into a single Telegram chat with `From <name>:` prefixes, and turns Telegram messages back into outbound iMessage sends (replies, `/send`, `/<contact-slug>`, photo uploads, inline whitelist search).

## Requirements

- chatwire >= 0.7.0 (installed and configured on macOS)
- Python >= 3.10
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Install

```bash
# If chatwire is managed via pipx:
pipx inject chatwire chatwire-telegram

# Otherwise:
pip install chatwire-telegram
```

## Configure

Add to your `config.json` under `integrations.telegram`:

```json
{
  "integrations": {
    "telegram": {
      "enabled": true,
      "bot_token": "123456:abc...",
      "allowed_user_ids": [12345678]
    }
  }
}
```

The first `allowed_user_id` is the delivery target — all relayed iMessage events are sent to that Telegram chat.

Enable inline mode on your bot via BotFather (`/setinline`) for the whitelist search typeahead to work.

## Bot commands

| Command | Description |
|---|---|
| `/start` | Help / status |
| `/whoami` | Show your Telegram user_id and chat_id |
| `/handles` | Show relay scope (SELF handles + whitelist) |
| `/refresh_contacts` | Reload Contacts.app lookup |
| `/mute <duration>` | Silence relay (e.g. `30m`, `2h`, `1d`) |
| `/unmute` | Resume relay |
| `/send <handle> <body>` | Send an iMessage |
| `/whitelist` | List whitelisted handles and groups |
| `/whitelist_add <handle or name>` | Add to whitelist |
| `/whitelist_remove <handle or name>` | Remove from whitelist |
| `/check [handle or name]` | Show iMessage/SMS capability |
| `/<slug>` | Select a contact or group as sticky target |

## License

MIT
