# Release Archiving

Mirror upstream GitHub releases locally so assets survive upstream deletion.

## Options

### Gitea / Forgejo mirror mode

Set a repo as a "mirror" with a pull interval. Automatically syncs branches, tags, releases, and attached release assets. Provides a browseable web UI. Heaviest option but turnkey.

### Cron + `gh release download`

```bash
gh release download --repo owner/repo --pattern '*.deb' -D /archive/repo/
```

Run on a timer. Lightest approach — just files on disk. No web UI, no metadata beyond what's in the filenames.

### `github-backup` (Python)

CLI tool that archives releases, issues, PRs, wikis, comments to disk. More metadata than raw `gh` but still cron-driven, no web UI.

### Object storage + script

Download release assets to S3/Garage on a schedule. Good if you already have object storage infrastructure.
