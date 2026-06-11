"""Unified truncation utilities for the Claude instruction optimizer."""

import json
from pathlib import Path
from typing import Protocol, cast

import tiktoken

from inop.config import OptimizerConfig
from inop.engine.models import FileInfo


class _Tokenizer(Protocol):
    def encode(self, text: str) -> list[int]: ...


class TruncationManager:
    """Unified truncation management for files, content, and messages."""

    MIN_TRUNCATION_BUDGET_TOKENS: int = 1000

    def __init__(self, config: OptimizerConfig):
        self.config = config
        self._encoding: _Tokenizer = tiktoken.encoding_for_model(config.grader.model)

    def count_tokens(self, text: str) -> int:
        """Count tokens in text using the configured model encoding."""
        return len(self._encoding.encode(text))

    def truncate_text(self, text: str, max_length: int, suffix: str = "...") -> str:
        """Truncate text to max_length with optional suffix."""
        if len(text) <= max_length:
            return text
        return text[: max_length - len(suffix)] + suffix

    def _truncated_content(self, content: str, max_chars: int) -> str:
        """Helper to create truncated content with standard message."""
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n... [TRUNCATED: {len(content)} chars total, showing first {max_chars}]"

    def _skipped_content(self, content: str, threshold: int) -> str:
        """Helper to create skipped content message."""
        return f"[SKIPPED: File too large ({len(content)} chars > {threshold} limit)]"

    # ---- helpers extracted from truncate_files_by_tokens to reduce complexity ----
    def _count_files_tokens(self, files: list[dict[str, str]] | list[FileInfo]) -> int:
        payload = (
            [fi.model_dump() for fi in cast(list[FileInfo], files)]
            if files and isinstance(files[0], FileInfo)
            else files
        )
        return self.count_tokens(json.dumps(payload, indent=2))

    def _single_file_tokens(self, path: str, content: str) -> int:
        return self.count_tokens(json.dumps([{"path": path, "content": content}], indent=2))

    def _normalize_files(
        self, files: list[dict[str, str]] | list[FileInfo]
    ) -> list[tuple[str, str, dict[str, str] | FileInfo]]:
        out: list[tuple[str, str, dict[str, str] | FileInfo]] = []
        if files and isinstance(files[0], FileInfo):
            for fi in cast(list[FileInfo], files):
                out.append((fi.path, fi.content, fi))
        else:
            for d in cast(list[dict[str, str]], files):
                out.append((d["path"], d["content"], d))
        out.sort(key=lambda t: len(t[1]), reverse=True)
        return out

    def _binary_search_truncate(self, content: str, budget: int, path: str) -> tuple[str | None, int]:
        """Find longest truncated content that fits in token budget; return (content, tokens)."""
        lo, hi = 0, len(content)
        best_content: str | None = None
        best_tokens = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = self._truncated_content(content, mid)
            tokens = self._single_file_tokens(path, cand)
            if tokens <= budget:
                best_content, best_tokens = cand, tokens
                lo = mid + 1
            else:
                hi = mid - 1
        return (best_content, best_tokens) if best_content is not None else (None, 0)

    def truncate_file_content_by_size(
        self, files: list[FileInfo], max_size: int, purpose: str | None = None
    ) -> dict[str, str]:
        """Truncate file contents by character size.

        Args:
            files: List of FileInfo objects
            max_size: Maximum characters per file
            purpose: Optional label for logging/UX (ignored in logic)

        Returns:
            Dict mapping file paths to truncated content
        """
        truncated = {}
        skip_threshold = max_size * 5  # Skip extremely large files

        for file_info in files:
            path = file_info.path
            content = file_info.content

            if len(content) > skip_threshold:
                truncated[path] = self._skipped_content(content, skip_threshold)
            else:
                truncated[path] = self._truncated_content(content, max_size)

        return truncated

    def truncate_files_by_tokens(
        self, files_info: list[dict[str, str]] | list[FileInfo], max_tokens: int
    ) -> list[dict[str, str]] | list[FileInfo]:
        """Truncate files to fit within token budget using binary search.

        - Preserves element type (dict vs FileInfo)
        - Greedy: include whole files first (largest-to-smallest), then truncate when needed
        - Stops early if remaining budget is too small to make meaningful progress
        """
        # Fast-path: already within budget
        if self._count_files_tokens(files_info) <= max_tokens:
            return files_info

        normalized = self._normalize_files(files_info)
        dict_out: list[dict[str, str]] = []
        model_out: list[FileInfo] = []
        remaining = max_tokens

        for path, content, original in normalized:
            tokens = self._single_file_tokens(path, content)
            if tokens <= remaining:
                if isinstance(original, FileInfo):
                    model_out.append(original)
                else:
                    dict_out.append(original)
                remaining -= tokens
                continue

            # Too little budget left to attempt binary search meaningfully
            if remaining <= self.MIN_TRUNCATION_BUDGET_TOKENS:
                break

            truncated, used = self._binary_search_truncate(content, remaining, path)
            if truncated is None:
                # Can't fit even a tiny truncated slice; skip and try smaller files
                continue
            if isinstance(original, FileInfo):
                model_out.append(FileInfo(path=path, content=truncated))
            else:
                dict_out.append({"path": path, "content": truncated})
            remaining -= used

        result: list[dict[str, str]] | list[FileInfo]
        result = model_out if (files_info and isinstance(files_info[0], FileInfo)) else dict_out
        final = self._count_files_tokens(result)
        assert final <= max_tokens, f"File truncation failed: {final} tokens > {max_tokens} limit"
        return result

    def truncate_file_by_bytes(self, file_path: Path, max_bytes: int) -> str:
        """Read and truncate a single file by byte size.

        Args:
            file_path: Path to the file
            max_bytes: Maximum bytes to read

        Returns:
            File content, possibly truncated
        """
        try:
            file_size = file_path.stat().st_size

            if file_size > max_bytes:
                with file_path.open("r", encoding="utf-8") as f:
                    content = f.read(max_bytes)
                return self._truncated_content(content, len(content))  # Will show the truncation message
            return file_path.read_text()
        except UnicodeDecodeError:
            return "<<not a plaintext file>>"
