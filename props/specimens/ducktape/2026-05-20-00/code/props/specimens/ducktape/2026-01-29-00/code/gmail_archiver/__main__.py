"""CLI entry point for gmail-archiver."""

import asyncio
import base64
import itertools
import re
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Annotated

import typer
from googleapiclient.errors import HttpError
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from fmt_util.fmt_util import format_truncation_suffix
from gmail_archiver.cli.common import DryRunDefaultTrueOption, TokenFileOption, get_client
from gmail_archiver.cli.filters import filters_app
from gmail_archiver.cli.labels import labels_app
from gmail_archiver.dirs import get_cache_dir
from gmail_archiver.event_classifier import EmailTemplateExtractor
from gmail_archiver.gmail_api_models import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan, Planner
from gmail_archiver.plan_display import display_plan, summarize_plan
from gmail_archiver.planners.aliexpress import AliExpressPlanner
from gmail_archiver.planners.anthem_eob import AnthemEobPlanner
from gmail_archiver.planners.anthem_reimbursement import AnthemReimbursementPlanner
from gmail_archiver.planners.anthropic import AnthropicReceiptPlanner
from gmail_archiver.planners.dbsa import DbsaEventPlanner
from gmail_archiver.planners.doordash import DoorDashPlanner
from gmail_archiver.planners.one_medical import OneMedicalPlanner
from gmail_archiver.planners.spruce import SprucePlanner
from gmail_archiver.planners.square import SquarePlanner
from gmail_archiver.planners.usps import UspsPlanner

app = typer.Typer(help="Archive old Gmail emails based on extracted dates")
app.add_typer(filters_app, name="filters")
app.add_typer(labels_app, name="labels")


def print_error(console: Console, message: str) -> None:
    console.print(f"[bold red]Error:[/bold red] {message}")


@app.command()
def autoclean_inbox(dry_run: DryRunDefaultTrueOption = True, token_file: TokenFileOption = None):
    """Automatically clean up old emails from inbox.

    Adds 'gmail-archiver/inbox-auto-cleaned' label and removes from inbox.
    Only processes emails that are currently in the inbox.
    """
    console = Console()
    client = get_client(token_file)

    console.print("[bold]Gmail Email Archiver[/bold] - Inbox Auto-Cleaner")
    console.print(f"Dry run: {dry_run}, Archive label: gmail-archiver/inbox-auto-cleaned\n")

    inbox = GmailInbox(client, console, cache_dir=get_cache_dir())

    extractor = EmailTemplateExtractor()
    planners: list[Planner] = [
        AliExpressPlanner(),
        AnthropicReceiptPlanner(),
        DoorDashPlanner(),
        UspsPlanner(),
        DbsaEventPlanner(extractor),
        AnthemEobPlanner(),
        AnthemReimbursementPlanner(),
        SquarePlanner(),
        OneMedicalPlanner(),
        SprucePlanner(),
    ]

    plans = []
    for planner in planners:
        plan = planner.plan(inbox)
        plans.append(plan)

    combined = Plan.merge(plans)

    for msg in combined.messages:
        console.print(msg)
    console.print()

    display_plan(combined, inbox, console, dry_run=dry_run, group_by_category=True)

    # Use batched execution like cli/filters.py does
    if not dry_run and combined.count_operations() > 0:
        console.print("Executing label operations...")

        # Resolve label names -> IDs once (creating missing user labels) so batchModify
        # receives label IDs as required by the Gmail API. Previously we passed label
        # *names*, which triggered HttpError 400 "Invalid label: ..." when Gmail
        # validated the request body.
        label_name_to_id: dict[str, str] = {}

        system_label_ids = set(SystemLabel)

        def ensure_label_id(label_name: str) -> str:
            if label_name in label_name_to_id:
                return label_name_to_id[label_name]
            # System labels are already IDs; user labels need to exist to obtain an ID
            if label_name in system_label_ids:
                label_name_to_id[label_name] = label_name
            else:
                label_name_to_id[label_name] = client.get_or_create_label(label_name)
            return label_name_to_id[label_name]

        # Pre-resolve all labels we plan to touch to avoid surprises mid-batch
        for planned_action in combined.actions.values():
            for lbl in planned_action.action.labels_to_add | planned_action.action.labels_to_remove:
                ensure_label_id(lbl)

        # Execute each unique action signature in batches
        total_processed = 0
        for sig, msg_ids in combined.group_by_signature().items():
            batch_size = 1000
            add_ids = [ensure_label_id(lbl) for lbl in sig.labels_to_add]
            remove_ids = [ensure_label_id(lbl) for lbl in sig.labels_to_remove]

            for batch in itertools.batched(msg_ids, batch_size, strict=False):
                body: dict = {"ids": list(batch)}
                if add_ids:
                    body["addLabelIds"] = add_ids
                if remove_ids:
                    body["removeLabelIds"] = remove_ids

                client.service.users().messages().batchModify(userId="me", body=body).execute()
                total_processed += len(batch)

        console.print(f"[green]✓[/green] Executed {total_processed} label operation(s)")
    elif dry_run:
        console.print(f"[yellow]DRY RUN:[/yellow] Would perform {combined.count_operations()} label operation(s)")

    console.print(f"\n{summarize_plan(combined)}")


@app.command()
def download_matching(
    query: Annotated[str, typer.Argument(help="Gmail search query (e.g., 'label:inbox', 'from:example.com')")],
    output_dir: Annotated[Path, typer.Option("--output-dir", "-o", help="Directory to save downloaded emails")] = Path(
        "emails"
    ),
    max_results: Annotated[
        int | None, typer.Option("--max-results", "-n", help="Maximum number of emails to download")
    ] = None,
    batch_size: Annotated[
        int,
        typer.Option("--batch-size", "-b", help="Number of emails to fetch per batch (reduce if hitting rate limits)"),
    ] = 10,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
    token_file: TokenFileOption = None,
):
    """Download all emails matching a Gmail search query.

    Examples:
        gmail-archiver download-matching "label:inbox"
        gmail-archiver download-matching "from:example.com" -o ./example_emails
        gmail-archiver download-matching "is:unread" --max-results 10
    """
    console = Console()
    console.print(f"[bold]Searching for emails:[/bold] {query}\n")

    client = get_client(token_file)

    console.print("Searching Gmail...")
    all_message_ids = client.list_messages_by_query(query, max_results=max_results)

    if not all_message_ids:
        console.print("[yellow]No matching emails found.[/yellow]")
        return

    console.print(f"Found {len(all_message_ids)} matching emails.\n")

    if output_dir.exists():
        existing_files = list(output_dir.glob("*.eml"))
        existing_ids = {f.stem for f in existing_files}
        search_result_ids = set(all_message_ids)

        orphaned_ids = existing_ids - search_result_ids

        if orphaned_ids:
            console.print(f"[yellow]Found {len(orphaned_ids)} downloaded emails no longer in search results:[/yellow]")

            orphaned_list = sorted(orphaned_ids)[:10]
            for msg_id in orphaned_list:
                console.print(f"  - {msg_id}.eml")
            if suffix := format_truncation_suffix(len(orphaned_ids), 10):
                console.print(suffix)

            console.print()
            delete_confirm = typer.confirm("Delete these orphaned files?", default=False)

            if delete_confirm:
                for msg_id in orphaned_ids:
                    file_path = output_dir / f"{msg_id}.eml"
                    if file_path.exists():
                        file_path.unlink()
                console.print(f"[green]✓[/green] Deleted {len(orphaned_ids)} orphaned files.\n")
            else:
                console.print("[yellow]Skipped deletion.[/yellow]\n")

    console.print("[bold]Sample of emails to download:[/bold]")
    sample_table = Table()
    sample_table.add_column("#", style="cyan", width=4)
    sample_table.add_column("From", style="green", width=30)
    sample_table.add_column("Subject", style="yellow", width=50)
    sample_table.add_column("Date", style="magenta", width=20)

    sample_size = min(10, len(all_message_ids))
    sample_metadata = client.get_messages_metadata_batch(all_message_ids[:sample_size], batch_size=sample_size)

    for idx, msg in enumerate(sample_metadata, start=1):
        sample_table.add_row(str(idx), msg.sender[:30], msg.subject[:50], (msg.date_header or "")[:20])

    console.print(sample_table)
    if suffix := format_truncation_suffix(len(all_message_ids), sample_size, "emails"):
        console.print(suffix + "\n")

    existing_preview = {f.stem for f in output_dir.glob("*.eml")} if output_dir.exists() else set()
    to_download_count = len([msg_id for msg_id in all_message_ids if msg_id not in existing_preview])

    if not yes:
        if to_download_count < len(all_message_ids):
            confirm = typer.confirm(
                f"\nDownload {to_download_count} new emails to {output_dir}? ({len(all_message_ids) - to_download_count} already exist)"
            )
        else:
            confirm = typer.confirm(f"\nDownload {len(all_message_ids)} emails to {output_dir}?")

        if not confirm:
            console.print("[yellow]Download cancelled.[/yellow]")
            return

    output_dir.mkdir(parents=True, exist_ok=True)

    existing_message_ids: set[str] = {f.stem for f in output_dir.glob("*.eml")}
    to_download = [msg_id for msg_id in all_message_ids if msg_id not in existing_message_ids]

    if len(to_download) < len(all_message_ids):
        console.print(f"[dim]Skipping {len(all_message_ids) - len(to_download)} already downloaded emails.[/dim]")

    if not to_download:
        console.print("[green]✓[/green] All emails already downloaded!")
        return

    console.print(f"\n[bold]Downloading {len(to_download)} emails to {output_dir}...[/bold]\n")

    all_successful = []
    all_failed = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Downloading emails...", total=len(to_download))

        for i in range(0, len(to_download), batch_size):
            batch_ids = to_download[i : i + batch_size]

            raw_messages, errors = client.get_messages_raw_batch(batch_ids, batch_size=batch_size, retry_failures=True)

            all_successful.extend(raw_messages)
            all_failed.extend(errors)

            for message_id, raw_bytes in raw_messages:
                output_file = output_dir / f"{message_id}.eml"
                output_file.write_bytes(raw_bytes)
                progress.update(task, advance=1)

            progress.update(task, advance=len(errors))

    console.print(f"\n[bold green]✓[/bold green] Downloaded {len(all_successful)} emails to {output_dir}")

    if all_failed:
        console.print(f"\n[bold red]✗[/bold red] Failed to download {len(all_failed)} emails:")
        for msg_id, error in all_failed[:10]:  # Show first 10
            console.print(f"  - {msg_id}: {error[:80]}")
        if suffix := format_truncation_suffix(len(all_failed), 10, "failures"):
            console.print(suffix)


def parse_gmail_link(url: str) -> str | None:
    """Parse Gmail web link to extract message ID."""
    return match.group(1) if (match := re.search(r"/#[^/]+/([a-f0-9]{16})", url)) else None


@app.command()
def download_email(
    id_or_link: Annotated[str, typer.Argument(help="Gmail message ID, thread ID, or Gmail web link")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output .eml file path (default: <id>.eml in current directory)"),
    ] = None,
    token_file: TokenFileOption = None,
):
    """Download an email as .eml file by message ID, thread ID, or Gmail link.

    Supports various Gmail link formats:
        - https://mail.google.com/mail/u/0/#inbox/19b1c55967d81057 (message ID)
        - https://mail.google.com/mail/u/0/#inbox/FMfcgzQcqtXwNDFwNDZXFhFgVnXRzQjw (thread ID)
        - https://mail.google.com/mail/#all/19b1c55967d81057

    Examples:
        gmail-archiver download-email 19b1c55967d81057
        gmail-archiver download-email FMfcgzQcqtXwNDFwNDZXFhFgVnXRzQjw
        gmail-archiver download-email https://mail.google.com/mail/u/0/#inbox/FMfcgzQcqtXwNDFwNDZXFhFgVnXRzQjw
        gmail-archiver download-email 19b1c55967d81057 -o my_email.eml
    """
    console = Console()
    extracted_id: str | None = id_or_link
    if id_or_link.startswith("http"):
        extracted_id = parse_gmail_link(id_or_link)
        if not extracted_id:
            print_error(console, f"Could not extract message ID from link: {id_or_link}")
            console.print(
                "\nNote: Gmail web URLs use encoded IDs that don't work with the Gmail API. "
                "To download an email, find its message ID by searching Gmail with the download-matching command."
            )
            raise typer.Exit(code=1)
        console.print(f"Extracted message ID: {extracted_id}")

    assert extracted_id is not None

    client = get_client(token_file)

    message_id = extracted_id
    console.print(f"Downloading message {message_id}...")

    try:
        msg = client.service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    except HttpError as e:
        if e.resp.status == 404:
            console.print("Message not found, trying as thread ID...")
            try:
                thread = client.service.users().threads().get(userId="me", id=extracted_id, format="minimal").execute()
                messages = thread.get("messages", [])

                if not messages:
                    print_error(console, f"Thread {extracted_id} has no messages")
                    raise typer.Exit(code=1)

                message_id = messages[0]["id"]
                console.print(f"Found thread with {len(messages)} message(s), downloading first: {message_id}")

                msg = client.service.users().messages().get(userId="me", id=message_id, format="raw").execute()
            except HttpError as thread_error:
                print_error(console, f"Failed to fetch as message or thread: {thread_error}")
                raise typer.Exit(code=1)
        else:
            print_error(console, f"Failed to download message {message_id}: {e}")
            raise typer.Exit(code=1)

    output = output or Path(f"{message_id}.eml")

    raw_bytes = base64.urlsafe_b64decode(msg["raw"])
    parsed = BytesParser(policy=default).parsebytes(raw_bytes)

    output.write_bytes(raw_bytes)

    console.print(f"[bold green]✓[/bold green] Downloaded email to {output}")

    console.print("\n[bold]Email details:[/bold]")
    console.print(f"  From: {parsed.get('From', '')}")
    console.print(f"  To: {parsed.get('To', '')}")
    console.print(f"  Subject: {parsed.get('Subject', '')}")
    console.print(f"  Date: {parsed.get('Date', '')}")


@app.command()
def classify_event(
    eml_file: Annotated[Path, typer.Argument(help="Path to .eml file to classify", exists=True)],
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Skip cache and re-classify")] = False,
):
    """Recognize email template and extract structured data using OpenAI.

    This command is for debugging the template extractor. It reads an .eml file,
    extracts the subject/body/date, and uses OpenAI to determine which template
    (if any) the email matches and extracts structured data accordingly.

    Example:
        gmail-archiver classify-event inbox/19b18706c1c9ae8f.eml
    """
    console = Console()
    console.print(f"Reading {eml_file}...")

    # Read raw bytes from .eml file
    raw_bytes = eml_file.read_bytes()

    # Create Email object from raw bytes (using dummy metadata for local .eml)
    message_id = eml_file.stem
    email = Email(
        id=message_id, thread_id=None, label_ids=[], internal_date=datetime.now(UTC), snippet=None, raw_bytes=raw_bytes
    )

    console.print("\n[bold]Email Details:[/bold]")
    console.print(f"  Message ID: {message_id}")
    console.print(f"  Subject: {email.subject}")
    console.print(f"  Date: {email.date}")
    console.print(f"  Body length: {len(email.get_text())} chars\n")

    async def extract():
        extractor = EmailTemplateExtractor()
        return await extractor.extract(email, use_cache=not no_cache)

    console.print("[bold]Extracting with OpenAI...[/bold]")
    result = asyncio.run(extract())

    console.print("\n[bold]Template Recognition Result:[/bold]")
    console.print_json(data=result.model_dump(mode="json"))

    cache_status = "skipped" if no_cache else "used/saved"
    console.print(f"\n[dim]Cache: {cache_status}[/dim]")


if __name__ == "__main__":
    app()
