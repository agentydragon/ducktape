"""Refresh-token storage backed by the user's Secret Service keyring."""

from __future__ import annotations

import asyncio


class SecretServiceTokenStore:
    """Store only the long-lived OAuth refresh token in Secret Service."""

    _ATTRIBUTES = ("application", "plaid-spend-desktop", "account", "default")
    _LABEL = "Plaid Spend desktop refresh token"

    async def load_refresh_token(self) -> str | None:
        process = await asyncio.create_subprocess_exec(
            "secret-tool",
            "lookup",
            *self._ATTRIBUTES,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return None
        token = stdout.decode("utf-8").strip()
        return token or None

    async def save_refresh_token(self, token: str) -> None:
        process = await asyncio.create_subprocess_exec(
            "secret-tool",
            "store",
            f"--label={self._LABEL}",
            *self._ATTRIBUTES,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(token.encode("utf-8"))
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"could not store Plaid Spend credentials in Secret Service: {detail}")

    async def clear(self) -> None:
        process = await asyncio.create_subprocess_exec(
            "secret-tool", "clear", *self._ATTRIBUTES, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"could not remove Plaid Spend credentials from Secret Service: {detail}")
