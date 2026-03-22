# Gmail Email Archiver

Auto-archive old Gmail emails based on extracted dates from email content. Uses Gmail filters (managed via YAML config) to label emails, then periodically scans and archives old ones.

**V0 Scope**: Anthropic API receipts, USPS Informed Delivery, and other receipt types.

## Setup

Requires Gmail API credentials in `~/.gmail-mcp/` (`gcp-oauth.keys.json` + `token.json`).

## Usage

```bash
gmail-archiver autoclean-inbox              # Preview (dry-run)
gmail-archiver autoclean-inbox --no-dry-run # Archive for real
gmail-archiver filters sync filters.yaml    # Sync filters to Gmail
gmail-archiver filters diff filters.yaml    # Preview filter changes
gmail-archiver filters apply filters.yaml   # Apply filter labels to existing emails
gmail-archiver filters download             # Export Gmail filters to YAML
```

### Cleanup Rules

| Category               | Label                          | Threshold |
| ---------------------- | ------------------------------ | --------- |
| Anthropic receipts     | `receipts/anthropic`           | 30 days   |
| USPS Informed Delivery | `batch/usps-informed-delivery` | 7 days    |

Archived emails get `gmail-archiver/inbox-auto-cleaned` label and are removed from inbox.

## Architecture

- **Planners** (`gmail_archiver/planners/`): category-specific cleanup logic (parser + planning)
- **Core** (`gmail_archiver/core.py`): `Plan`, `Planner` protocol, display helpers
- **Inbox** (`gmail_archiver/inbox.py`): cached Gmail access interface
- **Gmail Client** (`gmail_archiver/gmail_client.py`): Gmail API wrapper
- **Filter Models** (`gmail_archiver/gmail_yaml_filters_models.py`): Pydantic V2 models for filter YAML (compatible with [gmail-yaml-filters](https://github.com/mesozoic/gmail-yaml-filters))
- **Filter Sync** (`gmail_archiver/filter_sync.py`): filter normalization, diffing, CRUD

## TODO

- Strip PDFs and anonymize emails for test data
- Add more parsers (GitHub Sponsors, Stripe, etc.)
- Add logging, rate limit handling, retry logic
