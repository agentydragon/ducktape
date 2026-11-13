# Gmail Email Archiver

Manage Gmail filters from YAML and preview or archive eligible inbox messages with `autoclean-inbox`, which extracts cleanup dates from message content.

**V0 Scope**: Anthropic API receipts, USPS Informed Delivery, and other receipt types.

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

| Category               | Label                          | Threshold |
| ---------------------- | ------------------------------ | --------- |
| Anthropic receipts     | `receipts/anthropic`           | 30 days   |
| USPS Informed Delivery | `batch/usps-informed-delivery` | 7 days    |

Archived emails get `gmail-archiver/inbox-auto-cleaned` label and are removed from inbox.

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
