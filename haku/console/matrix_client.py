"""Thin Matrix client-API wrapper for the console's Matrix chat surface.

Only the calls the sync loop needs: log in, long-poll `/sync`, join a room, send a
message. Deliberately not a general Matrix SDK — the surface is small enough that a
dependency would cost more than it saves, and every call here is one the requirements
name (`haku/plans/matrix_chat_runtime.md`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Long-poll ceiling. Synapse returns as soon as anything arrives, so this only bounds how
# long a quiet connection stays open before it is re-established.
SYNC_TIMEOUT_MS = 30_000


class MatrixAuthError(Exception):
    """The homeserver rejected our access token.

    Raised so the caller can re-login rather than treating it as a transport failure:
    Synapse invalidates tokens on password set and on restore from an older backup, and
    the console is expected to recover by logging in again (R10.3a).
    """


@dataclass(frozen=True)
class InboundMessage:
    """One `m.room.message` addressed to us."""

    room_id: str
    event_id: str
    sender: str
    body: str
    origin_server_ts: int


@dataclass(frozen=True)
class Invite:
    """A pending room invitation and who issued it."""

    room_id: str
    inviter: str


@dataclass(frozen=True)
class SyncResult:
    next_batch: str
    messages: tuple[InboundMessage, ...]
    invites: tuple[Invite, ...]


class MatrixClient:
    """Authenticated Matrix client-API calls against one homeserver."""

    def __init__(self, http: httpx.AsyncClient, homeserver: str, user_id: str):
        self._http = http
        self._base = homeserver.rstrip("/")
        self._user_id = user_id

    async def login(self, password: str, device_id: str) -> str:
        """Password login, returning an access token.

        `device_id` is pinned so repeated logins reuse one device instead of leaving a
        new one behind on every restart.
        """
        resp = await self._http.post(
            f"{self._base}/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": self._user_id},
                "password": password,
                "device_id": device_id,
            },
        )
        resp.raise_for_status()
        return str(resp.json()["access_token"])

    async def whoami(self, token: str) -> bool:
        """True if `token` still authenticates as us."""
        resp = await self._http.get(f"{self._base}/_matrix/client/v3/account/whoami", headers=self._auth(token))
        return resp.status_code == 200 and resp.json().get("user_id") == self._user_id

    async def sync(self, token: str, since: str | None) -> SyncResult:
        """One long-poll `/sync`, parsed down to what the loop acts on."""
        params: dict[str, Any] = {"timeout": SYNC_TIMEOUT_MS}
        if since is not None:
            params["since"] = since
        resp = await self._http.get(
            f"{self._base}/_matrix/client/v3/sync",
            params=params,
            headers=self._auth(token),
            timeout=httpx.Timeout(SYNC_TIMEOUT_MS / 1000 + 15),
        )
        self._raise_for_auth(resp)
        resp.raise_for_status()
        return self._parse_sync(resp.json())

    async def join(self, token: str, room_id: str) -> None:
        resp = await self._http.post(
            f"{self._base}/_matrix/client/v3/rooms/{_encode(room_id)}/join", json={}, headers=self._auth(token)
        )
        self._raise_for_auth(resp)
        resp.raise_for_status()

    async def send_text(self, token: str, room_id: str, body: str, txn_id: str) -> str:
        """Send a plain-text message, returning its event ID.

        `txn_id` makes the send idempotent: a retry with the same value is deduplicated by
        the homeserver rather than posting twice.
        """
        resp = await self._http.put(
            f"{self._base}/_matrix/client/v3/rooms/{_encode(room_id)}/send/m.room.message/{_encode(txn_id)}",
            json={"msgtype": "m.text", "body": body},
            headers=self._auth(token),
        )
        self._raise_for_auth(resp)
        resp.raise_for_status()
        return str(resp.json()["event_id"])

    def _auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _raise_for_auth(self, resp: httpx.Response) -> None:
        if resp.status_code == 401:
            raise MatrixAuthError(resp.json().get("errcode", "M_UNKNOWN_TOKEN"))

    def _parse_sync(self, body: dict[str, Any]) -> SyncResult:
        messages: list[InboundMessage] = []
        for room_id, room in (body.get("rooms", {}).get("join") or {}).items():
            for event in room.get("timeline", {}).get("events") or []:
                if event.get("type") != "m.room.message":
                    continue
                # Never treat our own messages as input (R1.5). The event-ID filter that
                # backs this up lives in the sync service, which knows what it has sent.
                if event.get("sender") == self._user_id:
                    continue
                if (content := event.get("content") or {}).get("msgtype") != "m.text":
                    continue
                messages.append(
                    InboundMessage(
                        room_id=room_id,
                        event_id=event["event_id"],
                        sender=event["sender"],
                        body=str(content.get("body", "")),
                        origin_server_ts=int(event.get("origin_server_ts", 0)),
                    )
                )

        invites = [
            Invite(room_id=room_id, inviter=event["sender"])
            for room_id, room in (body.get("rooms", {}).get("invite") or {}).items()
            for event in room.get("invite_state", {}).get("events") or []
            if event.get("type") == "m.room.member"
            and event.get("state_key") == self._user_id
            and (event.get("content") or {}).get("membership") == "invite"
        ]

        return SyncResult(next_batch=str(body["next_batch"]), messages=tuple(messages), invites=tuple(invites))


def _encode(value: str) -> str:
    return quote(value, safe="")
