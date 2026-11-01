"""Read-only native Claude/Codex JSONL request accounting, run on a source host.

The output contains aggregates and audit counts only; transcript content never leaves
its source host. SQLite bounds memory while duplicate copied/forked records resolve.
"""

import argparse
import collections
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

UTC = dt.UTC
FIELDS = (
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def as_time(value):
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def counters(usage, provider):
    if not isinstance(usage, dict):
        return None

    def get(*names):
        for name in names:
            value = usage.get(name)
            if type(value) is int and value >= 0:
                return value
        return None

    raw_input = get("input_tokens", "inputTokens")
    read = get("cache_read_input_tokens", "cached_input_tokens", "cacheReadInputTokens")
    write = get("cache_creation_input_tokens", "cache_write_input_tokens", "cacheCreationInputTokens")
    output = get("output_tokens", "outputTokens")
    details = usage.get("output_tokens_details")
    reasoning = get("reasoning_output_tokens", "thinkingTokens")
    if reasoning is None and isinstance(details, dict):
        value = details.get("thinking_tokens")
        reasoning = value if type(value) is int and value >= 0 else None
    if provider == "openai" and raw_input is not None and read is not None:
        uncached = raw_input - read
        if uncached < 0:
            return None
    else:
        uncached = raw_input
    total = get("total_tokens")
    return (uncached, read, write, output, reasoning, total)


def counter_delta(current, previous):
    if current is None or previous is None or any(value is None for value in current + previous):
        return None
    return tuple(int(after) - int(before) for after, before in zip(current, previous, strict=True))


def database(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT, source TEXT NOT NULL, lane TEXT NOT NULL, ts TEXT, uncached INTEGER, read INTEGER, write INTEGER, output INTEGER, reasoning INTEGER, total INTEGER, rank INTEGER NOT NULL)"
    )
    return db


def scan(machine, home, db):
    audit: collections.Counter[str] = collections.Counter()
    span = {"oldest": None, "newest": None}
    roots = (
        (home / ".claude/projects", "anthropic"),
        (home / ".codex/sessions", "openai"),
        (home / ".codex/archived_sessions", "openai"),
    )
    for root, provider in roots:
        files = sorted(root.rglob("*.jsonl")) if root.exists() else []
        audit[f"{root.name}_files"] = len(files)
        audit[f"{root.name}_bytes_at_scan"] = sum(p.stat().st_size for p in files)
        for path in files:
            context_model = None
            direct_by_turn = set()
            snapshots = []
            try:
                with path.open("rb") as stream:
                    for lineno, line in enumerate(stream, 1):
                        audit["lines"] += 1
                        # Most journal lines carry large prompts, output, or encrypted state.
                        if not any(
                            marker in line
                            for marker in (
                                b'"usage"',
                                b'"last_token_usage"',
                                b'"turn_context"',
                                b'"thread_settings_applied"',
                                b'"session_meta"',
                                b'"result"',
                            )
                        ):
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError, UnicodeDecodeError:
                            audit["candidate_parse_errors"] += 1
                            continue
                        if not isinstance(record, dict):
                            continue
                        typ = record.get("type")
                        payload = record.get("payload")
                        if provider == "openai" and isinstance(payload, dict):
                            if typ == "turn_context":
                                context_model = payload.get("model") or context_model
                            elif typ == "event_msg" and payload.get("type") == "thread_settings_applied":
                                settings = payload.get("thread_settings")
                                if isinstance(settings, dict):
                                    context_model = settings.get("model") or context_model
                        ts = as_time(record.get("timestamp"))
                        if ts is not None:
                            stamp = ts.isoformat()
                            span["oldest"] = min(span["oldest"], stamp) if span["oldest"] else stamp
                            span["newest"] = max(span["newest"], stamp) if span["newest"] else stamp
                        else:
                            stamp = None
                        if provider == "anthropic":
                            if typ == "assistant":
                                msg = record.get("message")
                                if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                                    values = counters(msg["usage"], provider)
                                    if values:
                                        ident = msg.get("id")
                                        key = (
                                            "anthropic:" + ident
                                            if isinstance(ident, str) and ident
                                            else f"anthropic:{machine}:{path}:{lineno}"
                                        )
                                        add(
                                            db,
                                            audit,
                                            key,
                                            provider,
                                            msg.get("model"),
                                            "claude_message",
                                            machine,
                                            stamp,
                                            values,
                                        )
                                        if ident is None:
                                            audit["claude_missing_message_id"] += 1
                            elif typ == "result":
                                audit["claude_result_records"] += 1
                                if isinstance(record.get("usage"), dict):
                                    audit["claude_result_with_usage"] += 1
                        elif isinstance(payload, dict):
                            if typ == "token_usage_record" and isinstance(payload.get("usage"), dict):
                                values = counters(payload["usage"], provider)
                                if values:
                                    rid = payload.get("response_id")
                                    key = (
                                        "openai:" + rid
                                        if isinstance(rid, str) and rid
                                        else f"openai:{machine}:{path}:{lineno}"
                                    )
                                    model = payload.get("model") or context_model
                                    add(db, audit, key, provider, model, "codex_response", machine, stamp, values)
                                    turn = payload.get("turn_id")
                                    if isinstance(turn, str):
                                        direct_by_turn.add(turn)
                                    if rid is None:
                                        audit["codex_missing_response_id"] += 1
                                    if model is None:
                                        audit["codex_missing_model"] += 1
                            elif typ == "event_msg" and payload.get("type") == "token_count":
                                info = payload.get("info")
                                if isinstance(info, dict):
                                    if isinstance(info.get("last_token_usage"), dict):
                                        values = counters(info["last_token_usage"], provider)
                                        if values:
                                            total = counters(info.get("total_token_usage"), provider)
                                            snapshots.append(
                                                (lineno, stamp, context_model, payload.get("turn_id"), values, total)
                                            )
                                    if isinstance(info.get("total_token_usage"), dict):
                                        audit["codex_cumulative_snapshots"] += 1
            except OSError:
                audit["file_read_errors"] += 1
                continue
            audit["files_read"] += 1
            previous_total = None
            previous_model = None
            for lineno, stamp, model, turn, values, total in snapshots:
                # A last-response snapshot paired with a direct record is a duplicate.
                if isinstance(turn, str) and turn in direct_by_turn:
                    audit["last_response_paired_direct"] += 1
                    previous_total, previous_model = total, model
                    continue
                if direct_by_turn and turn is None:
                    # Without an ID, this can repeat a direct counter. Keep it out of
                    # the additive ledger; preserve the uncertainty in audit counts.
                    audit["last_response_ambiguous_overlap"] += 1
                    previous_total, previous_model = total, model
                    continue
                delta = counter_delta(total, previous_total)
                if (
                    delta is not None
                    and delta == values
                    and delta[-1] > 0
                    and model is not None
                    and model == previous_model
                ):
                    fingerprint = hashlib.blake2b(
                        json.dumps((stamp, model, values, total), separators=(",", ":")).encode(), digest_size=16
                    ).hexdigest()
                    add(
                        db,
                        audit,
                        "openai:delta:" + fingerprint,
                        provider,
                        model,
                        "codex_validated_counter_delta",
                        machine,
                        stamp,
                        values,
                    )
                else:
                    if delta is not None and all(v == 0 for v in delta):
                        audit["last_response_repeated_cumulative"] += 1
                    elif delta is not None and any(v < 0 for v in delta):
                        audit["last_response_cumulative_reset"] += 1
                    else:
                        audit["last_response_unvalidated_transition"] += 1
                    add(
                        db,
                        audit,
                        f"openai:last:{machine}:{path}:{lineno}",
                        provider,
                        model,
                        "codex_last_response_candidate",
                        machine,
                        stamp,
                        values,
                    )
                previous_total, previous_model = total, model
            db.commit()
    return audit, span


def add(db, audit, key, provider, model, source, machine, stamp, values):
    audit[source + "_observations"] += 1
    if stamp is None:
        audit["missing_timestamp"] += 1
        return
    if not isinstance(model, str) or not model:
        audit["missing_model"] += 1
        model = None
    previous = db.execute(
        "SELECT model, uncached, read, write, output, reasoning, total, rank FROM requests WHERE key=?", (key,)
    ).fetchone()
    rank = (values[3] or 0) * 100 + sum(v is not None for v in values)
    if previous:
        audit["duplicate_identity"] += 1
        if previous[:7] != (model, *values):
            audit["duplicate_counter_or_model_conflict"] += 1
        if rank <= previous[-1]:
            return
    db.execute(
        "INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, provider, model, source, machine, stamp, *values, rank),
    )


def series(db, minutes):
    for row in db.execute(
        "SELECT provider, model, source, lane, ts, uncached, read, write, output, reasoning, total FROM requests ORDER BY ts"
    ):
        provider, model, source, lane, stamp, *values = row
        time = dt.datetime.fromisoformat(stamp)
        if minutes == "weekly":
            day = time.date() - dt.timedelta(days=time.weekday())
            bucket = dt.datetime.combine(day, dt.time(), UTC).isoformat()
        else:
            n = int(minutes)
            bucket = time.replace(minute=(time.minute // n) * n, second=0, microsecond=0).isoformat()
        yield (bucket, provider, model, source, lane, values)


def aggregate(db, output_dir):
    for period in ("5", "60", "weekly"):
        groups: dict[tuple[str, str, str | None, str, str, str, str], dict[str, Any]] = {}
        for bucket, source_provider, model, source, lane, values in series(db, period):
            if source == "claude_message":
                provider = "anthropic" if model is not None and model.startswith("claude-") else "unknown"
                basis = "model_family_inferred" if provider == "anthropic" else "unresolved_model_family"
            else:
                provider = source_provider
                basis = "codex_harness_inferred"
            key = (bucket, provider, model, source, lane, "unknown", basis)
            item = groups.setdefault(
                key, {"requests": 0, "tokens": dict.fromkeys(FIELDS, 0), "known_fields": dict.fromkeys(FIELDS, 0)}
            )
            item["requests"] += 1
            for name, value in zip(FIELDS, values, strict=True):
                if value is not None:
                    item["tokens"][name] += value
                    item["known_fields"][name] += 1
        result = []
        for key, value in sorted(groups.items(), key=lambda pair: tuple(str(part) for part in pair[0])):
            fields = {
                name: value["tokens"][name] if value["known_fields"][name] == value["requests"] else None
                for name in FIELDS
            }
            result.append(
                dict(
                    zip(
                        (
                            "bucket_start_utc",
                            "provider",
                            "model",
                            "source",
                            "machine",
                            "payment_lane",
                            "provider_basis",
                        ),
                        key,
                        strict=True,
                    ),
                    requests=value["requests"],
                    **fields,
                    known_fields=value["known_fields"],
                    quality=(
                        "unvalidated_snapshot_candidate"
                        if key[3] == "codex_last_response_candidate"
                        else "validated_counter_transition"
                        if key[3] == "codex_validated_counter_delta"
                        else "identity_deduplicated_direct"
                    ),
                )
            )
        cadence = period if period == "weekly" else ("5min" if period == "5" else "hourly")
        for name, rows in (
            ("native-usage", [row for row in result if row["source"] != "codex_last_response_candidate"]),
            (
                "native-last-response-candidates",
                [row for row in result if row["source"] == "codex_last_response_candidate"],
            ),
        ):
            (output_dir / f"{name}-{cadence}.json").write_text(json.dumps(rows, separators=(",", ":")) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--machine", required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--aggregate-only", action="store_true", help="Rebuild series from an existing local request ledger"
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with database(args.output_dir / "native-accounting.sqlite") as db:
        if not args.aggregate_only:
            audit, span = scan(args.machine, args.home, db)
        aggregate(db, args.output_dir)
        if not args.aggregate_only:
            audit["unique_ledger_candidates"] = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            audit["source_counts"] = dict(db.execute("SELECT source, COUNT(*) FROM requests GROUP BY source"))
    coverage_path = args.output_dir / "native-coverage.json"
    coverage = (
        json.loads(coverage_path.read_text())
        if args.aggregate_only
        else {
            "machine": args.machine,
            "audit": audit,
            "span": span,
            "read_finished_utc": dt.datetime.now(UTC).isoformat(),
        }
    )
    coverage["normalization"] = {
        "anthropic": "Input is uncached input plus cache read plus cache write; output is separate. Source has no total counter, so total_tokens remains null.",
        "openai": "Input includes cached input. uncached_input_tokens = input_tokens - cached_input_tokens. Reasoning output is a subset of output. Source total is input plus output.",
        "provider_attribution": "Claude-family model names infer Anthropic; other model families within Claude journals are unresolved. Codex harness infers OpenAI but per-response routing was not captured. Both attribution bases are explicit in rows.",
        "payment_lane": "Native token records do not establish subscription versus API key; unknown is explicit.",
        "unknown_fields": "A bucket field is null unless every counted request supplied that category; known_fields counts its observed requests.",
        "codex_snapshot_policy": "Direct response_id counters and positive six-field last-response/cumulative transitions with unchanged model are in native-usage, under separate source labels. First, reset, mismatched and repeated snapshots are separate unvalidated candidates. Snapshots that may overlap direct records are audited and excluded.",
    }
    coverage_path.write_text(json.dumps(coverage, indent=2) + "\n")


if __name__ == "__main__":
    main()
