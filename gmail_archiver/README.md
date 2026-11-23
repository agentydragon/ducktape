# Gmail Email Archiver

Manage Gmail filters from YAML and preview or archive eligible inbox messages with `autoclean-inbox`, which extracts cleanup dates from message content.

## Setup

Gmail API commands require a Google authorized-user OAuth token. By default, they read
`~/.gmail-mcp/token.json`; pass `--token-file` to use a different file.

## Usage

```bash
gmail-archiver autoclean-inbox              # Preview (dry-run)
gmail-archiver autoclean-inbox --no-dry-run # Archive for real
gmail-archiver filters sync filters.yaml    # Sync filters to Gmail
gmail-archiver filters diff filters.yaml    # Preview filter changes
gmail-archiver filters list                # Find a Gmail filter ID
gmail-archiver filters apply FILTER_ID     # Apply that filter to existing emails
gmail-archiver filters download             # Export Gmail filters to YAML
```

### Cleanup Rules

`autoclean_inbox` in <gmail_archiver/main.py> registers every cleanup planner; each holds its Gmail query and archive rule.

Archived emails leave the inbox and get a label under `gmail-archiver/`: `gmail-archiver/inbox-auto-cleaned` unless the planner sets its own.

## Implementation

Cleanup planners live in `planners/`; filter synchronization uses the YAML format compatible with
[gmail-yaml-filters](https://github.com/mesozoic/gmail-yaml-filters).

Filter exports retain Gmail's `has_attachment`, `exclude_chats`, `size_bytes`, and
`size_comparison` criteria. `size_bytes` is distinct from the legacy YAML `size`
search operator. Manual `filters apply` accepts attachment presence and paired
`size_bytes`/`size_comparison` criteria; it rejects criteria without a verified
equivalent Gmail search query before changing messages.

## TODO

- Strip PDFs and anonymize emails for test data
- Add more parsers (GitHub Sponsors, Stripe, etc.)
