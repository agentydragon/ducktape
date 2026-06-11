"""Unified truncation utilities for the Claude instruction optimizer."""

import json
from pathlib import Path
from typing import Protocol

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
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n... [TRUNCATED: {len(content)} chars total, showing first {max_chars}]"

    def _skipped_content(self, content: str, threshold: int) -> str:
        return f"[SKIPPED: File too large ({len(content)} chars > {threshold} limit)]"

    def _count_files_tokens(self, files: list[FileInfo]) -> int:
        return self.count_tokens(json.dumps([f.model_dump() for f in files], indent=2))

    def _single_file_tokens(self, path: str, content: str) -> int:
        return self.count_tokens(json.dumps([{"path": path, "content": content}], indent=2))

    def _binary_search_truncate(self, content: str, budget: int, path: str) -> tuple[str | None, int]:
        """Find longest truncated content that fits in token budget."""
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
        """Truncate file contents by character size."""
        truncated = {}
        skip_threshold = max_size * 5

        for file_info in files:
            if len(file_info.content) > skip_threshold:
                truncated[file_info.path] = self._skipped_content(file_info.content, skip_threshold)
            else:
                truncated[file_info.path] = self._truncated_content(file_info.content, max_size)

        return truncated

    def truncate_files_by_tokens(self, files_info: list[FileInfo], max_tokens: int) -> list[FileInfo]:
        """Truncate files to fit within token budget using binary search.

        Greedy: include whole files first (largest-to-smallest), then truncate when needed.
        Stops early if remaining budget is too small to make meaningful progress.
        """
        if self._count_files_tokens(files_info) <= max_tokens:
            return files_info

        sorted_files = sorted(files_info, key=lambda f: len(f.content), reverse=True)
        result: list[FileInfo] = []
        remaining = max_tokens

        for fi in sorted_files:
            tokens = self._single_file_tokens(fi.path, fi.content)
            if tokens <= remaining:
                result.append(fi)
                remaining -= tokens
                continue

            if remaining <= self.MIN_TRUNCATION_BUDGET_TOKENS:
                break

            truncated, used = self._binary_search_truncate(fi.content, remaining, fi.path)
            if truncated is None:
                continue
            result.append(FileInfo(path=fi.path, content=truncated))
            remaining -= used

        final = self._count_files_tokens(result)
        assert final <= max_tokens, f"File truncation failed: {final} tokens > {max_tokens} limit"
        return result

    def truncate_file_by_bytes(self, file_path: Path, max_bytes: int) -> str:
        """Read and truncate a single file by byte size."""
        try:
            file_size = file_path.stat().st_size

            if file_size > max_bytes:
                with file_path.open("r", encoding="utf-8") as f:
                    content = f.read(max_bytes)
                return self._truncated_content(content, len(content))
            return file_path.read_text()
        except UnicodeDecodeError:
            return "<<not a plaintext file>>"
