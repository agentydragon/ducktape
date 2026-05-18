# Telegram Export Options (Selective)

When you want to export only specific groups/channels, not your entire Telegram history.

## API Credentials (prerequisite for all options below except built-in takeout)

Credentials and provisioning instructions live in <../secrets/shared/telegram-api.yaml> (SOPS-encrypted). Run `sops -d secrets/shared/telegram-api.yaml` to read them, or `sops edit ...` to rotate.

## Recommended: tg-archive

Exports specific Telegram groups to static websites (like mailing list archives).

- **Project:** [knadh/tg-archive](https://github.com/knadh/tg-archive)
- **Install:** `uv pip install tg-archive`

### Setup (One-time)

1. **Create a new site:**

   ```bash
   tg-archive --new --path=mysite
   cd mysite
   # Edit config.yaml to specify your group username or ID
   # Add api_id and api_hash from the prerequisite section above
   ```

2. **Authenticate your account:**

   ```bash
   tg-archive --sync
   ```

   First run prompts for:
   - Phone number (+countrycode number)
   - Telegram verification code (sent to your app)
   - Optionally 2FA password if enabled

   Creates `session.session` - keep this private, it's your logged-in session

3. **Build and export:**
   ```bash
   tg-archive --sync    # Syncs new messages (resume anytime)
   tg-archive --build   # Generates static site in ./site
   # Publish ./site anywhere
   ```

### Features

- Incremental sync (only new messages since last run)
- Downloads media locally
- SQLite database (`data.sqlite`)
- Static HTML output (year/month/day indexes, threaded replies)
- RSS/Atom feed support
- Polls, attachments, avatars

## Quick One-Offs: Telethon CLI

Export any chat to JSON without building a website.

```bash
# Install
uv pip install telethon-cli

# Authenticate (prompts for phone + verification code)
telethon-cli login --api-id API_ID --api-hash API_HASH --session myname

# Export specific chat to JSON
telethon-cli messages get-messages --session myname --entity chat_username --output json > messages.json
```

- **Good for:** Quick dumps, data analysis, custom processing
- **Docs:** [Telethon documentation](https://docs.telethon.dev/)

## Custom Scripts: Telethon/Pyrogram

Build your own export logic for full control.

```python
from telethon.sync import TelegramClient
import asyncio

async def export_chat():
    client = TelegramClient('session', api_id, api_hash)
    await client.start(phone)  # Prompts for phone + verification code

    async for message in client.iter_messages(chat_id):
        # Save to your format/database
        pass
```

- **Telethon:** [docs.telethon.dev](https://docs.telethon.dev/) - MTProto client
- **Pyrogram:** [docs.pyrogram.org](https://docs.pyrogram.org/) - Similar alternative

## Built-in Takeout (Not Recommended)

Telegram's official export tool at `Settings > Advanced > Export Telegram Data`.

- **Problem:** Exports everything (all chats, channels, contacts) - gigabytes
- **Good for:** Full account backups, legal requirements
- **Docs:** [core.telegram.org/api/takeout](https://core.telegram.org/api/takeout)

## Summary

| Tool             | Selective? | Output      | Best For                    |
| ---------------- | ---------- | ----------- | --------------------------- |
| tg-archive       | ✓          | Static HTML | Public archives, browsing   |
| Telethon CLI     | ✓          | JSON        | Quick dumps, analysis       |
| Custom script    | ✓          | Any         | Full control, custom format |
| Built-in takeout | ✗          | HTML/JSON   | Full backups only           |

**Recommendation:** Use **tg-archive** for browsing archives, **Telethon CLI** for data dumps.
