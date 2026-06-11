# Gmail Email Archiver

Auto-archive old Gmail emails based on extracted dates from email content.

## Overview

This tool:

1. Uses Gmail filters to auto-label incoming emails (managed via YAML config)
2. Periodically scans labeled emails and extracts dates from content
3. For emails past a threshold (e.g., >30 days), adds a secondary label and removes from inbox

**V0 Scope**: Anthropic API receipts only

## Setup

### 1. Build with Bazel

```bash
bazel build //gmail-archiver:gmail_archiver
```

### 2. Gmail API credentials

This tool uses Gmail API credentials from `~/.gmail-mcp/`. Ensure you have:

- `~/.gmail-mcp/gcp-oauth.keys.json` - OAuth client credentials
- `~/.gmail-mcp/token.json` - OAuth token (generated after first auth)

### 3. Set up Gmail filters

Sync filters from YAML to Gmail:

```bash
gmail-archiver filters sync filters.yaml
```

This will:

1. Show a diff of filters to create/delete
2. Prompt for confirmation before making changes

Other filter commands:

```bash
gmail-archiver filters diff filters.yaml      # Preview changes (read-only)
gmail-archiver filters upload filters.yaml    # Create new filters only (no deletions)
gmail-archiver filters download               # Export existing Gmail filters to YAML
gmail-archiver filters apply filters.yaml     # Apply filter labels to existing emails
```

## Usage

The `gmail-archiver` command-line tool provides several commands:

### Auto-clean old emails from inbox

Preview what would be archived (dry-run by default):

```bash
gmail-archiver autoclean-inbox
```

This command runs multiple cleanup rules:

**Anthropic receipts** (30 days):

- Label: `receipts/anthropic`
- Extracts payment date from email content
- Archives 30 days after payment

**USPS Informed Delivery** (7 days):

- Label: `batch/usps-informed-delivery`
- Uses email sent date
- Archives 7 days after delivery notification

For all emails:

- Adds `gmail-archiver/inbox-auto-cleaned` label
- Removes from inbox (keeps all other labels)
- Only processes emails currently in inbox

### Archive for real

```bash
gmail-archiver autoclean-inbox --no-dry-run
```

### Export existing Gmail filters to YAML

```bash
gmail-archiver filters download
```

This creates `filters_exported.yaml` which you can review and merge with your `filters.yaml`.

Custom output path:

```bash
gmail-archiver filters download -o my_filters.yaml
```

### Apply filter to existing emails

Gmail filters only apply to new incoming emails. To apply a filter's actions to existing emails that match its criteria:

```bash
gmail-archiver filters apply filters.yaml
```

This will:

1. Read `filters.yaml` and find applicable filters
2. Search for matching emails
3. Show a preview of what would be changed (dry-run by default)

To actually apply (interactive prompt by default):

```bash
gmail-archiver filters apply filters.yaml --no-dry-run
```

Apply only a specific filter:

```bash
gmail-archiver filters apply filters.yaml --label "receipts/anthropic"
```

## Development

See root `AGENTS.md` for Bazel basics (build, test, lint, adding dependencies).

## Architecture

- **Planners** (`gmail_archiver/planners/`): Category-specific cleanup logic
  - Each planner contains a parser (extracts dates/amounts) and planning logic (decides what to archive)
  - Examples: Anthropic receipts, USPS deliveries, Square receipts, insurance EOBs, etc.

- **Core** (`gmail_archiver/core.py`): Planning and execution abstractions
  - `Plan`: Represents planned actions (add/remove labels) for a set of emails
  - `Planner`: Protocol for implementing cleanup categories
  - `display_plan()` and `summarize_plan()`: View layer for showing plans

- **Inbox** (`gmail_archiver/inbox.py`): Cached Gmail access interface
  - Wraps Gmail API client with caching
  - Planners use this to fetch messages

- **Gmail Client** (`gmail_archiver/gmail_client.py`): Wrapper around Google Gmail API
  - List messages by label or query
  - Fetch message contents
  - Add/remove labels
  - Create labels

- **Filter Models** (`gmail_archiver/gmail_yaml_filters_models.py`): Pydantic V2 models for filter YAML
  - Compatible with [gmail-yaml-filters](https://github.com/mesozoic/gmail-yaml-filters) format
  - Used for parsing and generating filter YAML files
  - Provides type safety and validation for filter configurations

- **Filter Sync** (`gmail_archiver/filter_sync.py`): Filter synchronization logic
  - Normalizes Gmail API filters and YAML rules for comparison
  - Computes diffs between local YAML and remote Gmail filters
  - Handles label creation and filter CRUD operations

## Dependencies

Keep dependencies minimal. Current stack:

- `google-api-python-client` - Gmail API
- `pydantic` - Data models
- `beautifulsoup4` - HTML parsing
- `python-dateutil` - Date parsing
- `openai` - AI parsing (DBSA only)

## TODO

- Strip PDFs and anonymize emails for test data
- Add more parsers (e.g., GitHub Sponsors, Stripe, etc.)
- Make parser selection configurable via CLI
- Add logging
- Handle API rate limits
- Add retry logic for transient failures
