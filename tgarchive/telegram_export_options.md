# Telegram Export Options (Selective)

When you want to export only specific groups/channels, not your entire Telegram history.

## API credentials

API credentials live in the SOPS-encrypted <../secrets/shared/telegram-api.yaml>.
The repository's `tg.sh` and `tg_backup.py` read it directly; the other tools may
ask you to provide the API ID and hash. Keep the Telegram session file private too.

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

## Repository script: selective JSON and media

The repository includes [`tg_backup.py`](tg_backup.py), a Telethon Takeout
exporter for selected entities. It accepts `@username`s, numeric IDs, `t.me/...`
links, or exact dialog titles, and writes `entity.json`, `messages.json`, and
downloaded files under `media/` for each entity.

It reads credentials from the SOPS file above. Its default session (`session`)
and export (`backup/`) paths are relative to the current directory; run with
`tgarchive/` as the current directory to keep both under the existing
`.gitignore` rules. A completed run rewrites an entity's `entity.json` and
`messages.json`; use a new `--output` directory under `backup/` (for example,
`backup/another-snapshot`) to retain an older snapshot while keeping it ignored.
The export still contains private messages and media; `.gitignore` only
prevents accidental commits.

The script imports `telethon`, `PyYAML`, and `tqdm`. This tree is excluded from
Bazel and has no local Python dependency manifest; the root dependency manifest
does not declare `telethon`, so its runnable environment remains unverified.

## Built-in Takeout (Not Recommended)

Telegram's official export tool at `Settings > Advanced > Export Telegram Data`.

- **Problem:** Exports everything (all chats, channels, contacts) - gigabytes
- **Good for:** Full account backups, legal requirements
- **Docs:** [core.telegram.org/api/takeout](https://core.telegram.org/api/takeout)

## Summary

| Tool              | Selective? | Output       | Best For                       |
| ----------------- | ---------- | ------------ | ------------------------------ |
| tg-archive        | ✓          | Static HTML  | Public archives, browsing      |
| Telethon CLI      | ✓          | JSON         | Quick dumps, analysis          |
| Repository script | ✓          | JSON + media | Local export; setup unverified |
| Built-in takeout  | ✗          | HTML/JSON    | Full backups only              |

**Recommendation:** Use **tg-archive** for browsing archives, **Telethon CLI** for data dumps.
