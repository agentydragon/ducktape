"""Study Casino backend — REST + thin WebSocket state-changed pings.

Wire surface:

  GET  /state                           — full canonical state JSON (per user)
  POST /actions/session/complete        — commit an active session (timing supplied by client)
  POST /actions/session/add-past        — backfill a past session
  POST /actions/session/edit            — rename / re-time a completed session
  POST /actions/session/delete          — drop a completed session
  POST /actions/convert                 — credits → tokens
  POST /actions/prize/create            — add to user prize catalog
  POST /actions/prize/delete            — remove from user prize catalog
  POST /actions/prize/redeem            — spend tokens to redeem a prize
  POST /actions/import / reset          — bulk replace / wipe state (snapshot saved)
  POST /casino/slots/spin               — server-resolved slots
  POST /casino/roulette/spin            — server-resolved roulette
  POST /casino/blackjack/{deal,hit,stand,double} — server-resolved blackjack
  GET  /game-events / /ledger-events    — read-only audit listings
  GET  /me / /healthz                   — auth introspection / liveness
  WS   /ws                              — broadcasts {"type":"state_changed"}
                                          to every tab of the same user
                                          after a successful action; clients
                                          refetch `/state` on receipt.

Active study-session timer state lives in the browser's localStorage —
the server never sees an in-progress session. Only `/actions/session/complete`
(or `/actions/session/add-past`) ever inserts a row into the `sessions`
table.

Multi-user: each authenticated user gets a separate SQLite database
(`casino-<username>.db`). When OIDC is not configured the app falls back
to a single "default" user, keeping existing tests working.
"""

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from x.auragon_study_casino.actions import (
    ActionRequest,
    ActionResponse,
    AddPastSessionRequest,
    BlackjackDealRequest,
    BlackjackHandRequest,
    ConvertRequest,
    DeleteSessionRequest,
    EditSessionRequest,
    ImportRequest,
    PrizeCreateRequest,
    PrizeDeleteRequest,
    PrizeRedeemRequest,
    ResetRequest,
    RouletteSpinRequest,
    SessionCompleteRequest,
    SlotsSpinRequest,
)
from x.auragon_study_casino.auth import create_oidc_router, decode_session_token, make_current_user_dep
from x.auragon_study_casino.config import Settings
from x.auragon_study_casino.events import GameEventRead
from x.auragon_study_casino.games import (
    RNG_VERSION,
    SecretsRandom,
    dealer_play,
    draw_cards,
    hand_value,
    is_blackjack,
    make_shoe,
    public_blackjack_state,
    settle_blackjack,
    spin_roulette,
    spin_slots,
)
from x.auragon_study_casino.models import BalanceRow, BlackjackHandRow, PrizeLogRow, PrizeRow, SessionRow
from x.auragon_study_casino.store import ActionMutation, ActionRejectedError, SqlStore

logger = logging.getLogger(__name__)

# Only allow filesystem-safe characters in usernames to prevent path traversal.
_SAFE_USERNAME = re.compile(r"^[a-zA-Z0-9._@-]{1,64}$")


class _WSManager:
    """Per-user WebSocket registry for state_changed fan-out."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    def add(self, username: str, ws: WebSocket) -> None:
        self._connections[username].add(ws)

    def remove(self, username: str, ws: WebSocket) -> None:
        self._connections[username].discard(ws)

    async def push(self, username: str, message: dict, exclude: WebSocket | None = None) -> None:
        """Fan out `message` to every connected client for `username` except `exclude`."""
        dead: list[WebSocket] = []
        for ws in list(self._connections.get(username, ())):
            if ws is exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections[username].discard(ws)


def _credits(s: Session) -> int:
    balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1))
    assert balance is not None
    return balance.credits


def _tokens(s: Session) -> int:
    balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1))
    assert balance is not None
    return balance.tokens


def _balance(s: Session) -> BalanceRow:
    balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1).with_for_update())
    assert balance is not None
    return balance


def _require_credits(s: Session, amount: int) -> None:
    if amount <= 0:
        raise ActionRejectedError("invalid_wager", "wager must be positive")
    have = _credits(s)
    if have < amount:
        raise ActionRejectedError("insufficient_credits", f"need {amount} credits; have {have}")


def _session_minutes(row: SessionRow) -> int:
    return row.seconds // 60


def _mutate_blackjack_step(s: Session, hand_id: str, move: str, rng: SecretsRandom) -> ActionMutation:
    row = s.get(BlackjackHandRow, hand_id)
    if row is None or row.status != "playing":
        raise ActionRejectedError("blackjack_hand", "active blackjack hand not found")
    shoe = json.loads(row.shoe_json)
    player = json.loads(row.player_json)
    dealer = json.loads(row.dealer_json)
    current_wager = int(row.current_wager_credits)
    settlement = None

    if move == "hit":
        drawn, shoe = draw_cards(shoe, 1)
        player = [*player, *drawn]
        if hand_value(player) > 21:
            settlement = settle_blackjack(player, dealer, current_wager)
        elif hand_value(player) == 21:
            dealer, shoe = dealer_play(dealer, shoe)
            settlement = settle_blackjack(player, dealer, current_wager)
    elif move == "stand":
        dealer, shoe = dealer_play(dealer, shoe)
        settlement = settle_blackjack(player, dealer, current_wager)
    elif move == "double":
        if len(player) != 2:
            raise ActionRejectedError("blackjack_double", "double is only available on the first two cards")
        _require_credits(s, current_wager)
        balance = _balance(s)
        balance.credits -= current_wager
        current_wager *= 2
        drawn, shoe = draw_cards(shoe, 1)
        player = [*player, *drawn]
        if hand_value(player) <= 21:
            dealer, shoe = dealer_play(dealer, shoe)
        settlement = settle_blackjack(player, dealer, current_wager)
    else:
        raise ActionRejectedError("blackjack_move", f"unsupported blackjack move {move!r}")

    status = "done" if settlement is not None else "playing"
    if settlement is not None and settlement.payout_tokens:
        balance = _balance(s)
        balance.tokens += settlement.payout_tokens

    row.status = status
    row.updated_at_ms = int(time.time() * 1000)
    row.current_wager_credits = current_wager
    row.shoe_json = json.dumps(shoe, separators=(",", ":"))
    row.player_json = json.dumps(player, separators=(",", ":"))
    row.dealer_json = json.dumps(dealer, separators=(",", ":"))
    row.result_json = json.dumps(settlement.outcome, separators=(",", ":")) if settlement else None

    result = public_blackjack_state(
        hand_id=hand_id, status=status, player=player, dealer=dealer, current_wager=current_wager, settlement=settlement
    )
    game_event: dict[str, Any] | None = None
    if settlement is not None:
        game_event = {
            "game": "blackjack",
            "wager_credits": current_wager,
            "payout_tokens": settlement.payout_tokens,
            "outcome": settlement.outcome
            | {"initial_wager": row.wager_credits, "doubled": current_wager > row.wager_credits},
        }
    return ActionMutation(
        result=result,
        details={"hand_id": hand_id, "move": move},
        game_event=game_event,
        rng_version=RNG_VERSION if move in {"hit", "double"} else None,
    )


def create_app(settings: Settings) -> FastAPI:
    data_dir = settings.data_dir
    frontend_dist = settings.frontend_dist_dir or (Path(__file__).parent / "frontend" / "dist")

    # Per-user store registry. Keys are sanitised usernames; stores are
    # created lazily on first request for that user.
    stores: dict[str, SqlStore] = {}

    def get_store(username: str) -> SqlStore:
        if username not in stores:
            if not _SAFE_USERNAME.match(username):
                raise HTTPException(status_code=400, detail=f"invalid username: {username!r}")
            stores[username] = SqlStore(data_dir / f"casino-{username}.db")
        return stores[username]

    oidc = settings.oidc_config()
    current_user_dep = make_current_user_dep(oidc.session_secret if oidc else None)
    ws_manager = _WSManager()

    app = FastAPI(title="Study Casino", docs_url=None, redoc_url=None)
    app.state.current_user_dep = current_user_dep

    if oidc:
        app.include_router(
            create_oidc_router(
                issuer=oidc.issuer,
                client_id=oidc.client_id,
                client_secret=oidc.client_secret,
                session_secret=oidc.session_secret,
                public_url=oidc.public_url,
            )
        )

    async def commit_action(
        *,
        username: str,
        body: ActionRequest,
        action_type: str,
        mutator,
        snapshot_reason: str | None = None,
        snapshot_note: str | None = None,
    ) -> ActionResponse:
        store = get_store(username)
        try:
            result = await asyncio.to_thread(
                store.run_server_action,
                client_action_id=body.client_action_id,
                action_type=action_type,
                mutator=mutator,
                snapshot_reason=snapshot_reason,
                snapshot_note=snapshot_note,
            )
        except ActionRejectedError as e:
            raise HTTPException(status_code=409, detail={"rule": e.rule, "message": e.message}) from e

        await ws_manager.push(username, {"type": "state_changed"})
        return ActionResponse(
            client_action_id=body.client_action_id,
            event=result.event,
            result=result.result,
            game_event=result.game_event,
        )

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/me")
    def me(username: Annotated[str, Depends(current_user_dep)]) -> dict[str, str]:
        return {"username": username}

    @app.get("/state")
    def get_state(username: Annotated[str, Depends(current_user_dep)]) -> dict[str, Any]:
        store = get_store(username)
        return store.state_dump()

    @app.get("/game-events")
    def list_game_events(
        username: Annotated[str, Depends(current_user_dep)], limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[GameEventRead]:
        store = get_store(username)
        return store.list_game_events(limit=limit)

    @app.get("/ledger-events")
    def list_ledger_events(
        username: Annotated[str, Depends(current_user_dep)], limit: Annotated[int, Query(ge=1, le=500)] = 100
    ):
        store = get_store(username)
        return store.list_ledger_events(limit=limit)

    @app.post("/actions/session/complete")
    async def complete_session(
        body: SessionCompleteRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            session_id = body.session_id or f"session-{uuid.uuid4()}"
            if s.get(SessionRow, session_id) is not None:
                raise ActionRejectedError("session_id", "session id already exists")
            seconds = max(0, (body.ended_at_ms - body.start_time_ms - body.paused_duration_ms) // 1000)
            if seconds <= 0:
                return ActionMutation(result={"session_id": session_id, "seconds": 0, "credits_earned": 0})
            minutes = seconds // 60
            s.add(SessionRow(id=session_id, subject=body.subject, seconds=seconds, ended_at_ms=body.ended_at_ms))
            if minutes:
                _balance(s).credits += minutes
            return ActionMutation(
                result={"session_id": session_id, "seconds": seconds, "credits_earned": minutes},
                details={"subject": body.subject},
            )

        return await commit_action(username=username, body=body, action_type="session.complete", mutator=mutate)

    @app.post("/actions/session/add-past")
    async def add_past_session(
        body: AddPastSessionRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            session_id = body.session_id or f"manual-{uuid.uuid4()}"
            if s.get(SessionRow, session_id) is not None:
                raise ActionRejectedError("session_id", "session id already exists")
            s.add(SessionRow(id=session_id, subject=body.subject, seconds=body.seconds, ended_at_ms=body.ended_at_ms))
            credits_earned = body.seconds // 60
            if credits_earned:
                _balance(s).credits += credits_earned
            return ActionMutation(
                result={"session_id": session_id, "credits_earned": credits_earned},
                details={"subject": body.subject, "seconds": body.seconds},
            )

        return await commit_action(username=username, body=body, action_type="session.add_past", mutator=mutate)

    @app.post("/actions/session/edit")
    async def edit_session(
        body: EditSessionRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            row = s.get(SessionRow, body.session_id)
            if row is None:
                raise ActionRejectedError("session", "completed session not found")
            old_minutes = _session_minutes(row)
            if body.subject is not None:
                row.subject = body.subject
            if body.seconds is not None:
                row.seconds = body.seconds
            delta = _session_minutes(row) - old_minutes
            if delta:
                balance = _balance(s)
                balance.credits = max(0, balance.credits + delta)
            return ActionMutation(
                result={"session_id": body.session_id, "credits_delta": delta},
                details={"subject": row.subject, "seconds": row.seconds},
            )

        return await commit_action(username=username, body=body, action_type="session.edit", mutator=mutate)

    @app.post("/actions/session/delete")
    async def delete_session(
        body: DeleteSessionRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            row = s.get(SessionRow, body.session_id)
            if row is None:
                raise ActionRejectedError("session", "completed session not found")
            credits_delta = -_session_minutes(row)
            s.delete(row)
            balance = _balance(s)
            balance.credits = max(0, balance.credits + credits_delta)
            return ActionMutation(result={"session_id": body.session_id, "credits_delta": credits_delta})

        return await commit_action(username=username, body=body, action_type="session.delete", mutator=mutate)

    @app.post("/actions/convert")
    async def convert(body: ConvertRequest, username: Annotated[str, Depends(current_user_dep)]) -> ActionResponse:
        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            _require_credits(s, body.amount)
            balance = _balance(s)
            balance.credits -= body.amount
            balance.tokens += body.amount
            return ActionMutation(result={"amount": body.amount})

        return await commit_action(username=username, body=body, action_type="convert", mutator=mutate)

    @app.post("/actions/prize/create")
    async def create_prize(
        body: PrizeCreateRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            prize_id = body.prize_id or f"p{uuid.uuid4().hex[:12]}"
            if s.get(PrizeRow, prize_id) is not None:
                raise ActionRejectedError("prize_id", "prize id already exists")
            s.add(PrizeRow(id=prize_id, name=body.name, cost=body.cost))
            return ActionMutation(
                result={"prize_id": prize_id, "name": body.name, "cost": body.cost},
                details={"name": body.name, "cost": body.cost},
            )

        return await commit_action(username=username, body=body, action_type="prize.create", mutator=mutate)

    @app.post("/actions/prize/delete")
    async def delete_prize(
        body: PrizeDeleteRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            row = s.get(PrizeRow, body.prize_id)
            if row is None:
                raise ActionRejectedError("prize", "prize not found")
            s.delete(row)
            return ActionMutation(result={"prize_id": body.prize_id}, details={"name": row.name, "cost": row.cost})

        return await commit_action(username=username, body=body, action_type="prize.delete", mutator=mutate)

    @app.post("/actions/prize/redeem")
    async def redeem_prize(
        body: PrizeRedeemRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        def mutate(s: Session, now_ms: int) -> ActionMutation:
            prize = s.get(PrizeRow, body.prize_id)
            if prize is None:
                raise ActionRejectedError("prize", "prize not found")
            cost = prize.cost
            if cost <= 0:
                raise ActionRejectedError("prize", "prize cost must be positive")
            balance = _balance(s)
            if balance.tokens < cost:
                raise ActionRejectedError("insufficient_tokens", f"need {cost} tokens; have {balance.tokens}")
            balance.tokens -= cost

            redemption_id = f"r-{uuid.uuid4()}"
            s.add(PrizeLogRow(id=redemption_id, name=prize.name, cost=cost, at_ms=now_ms))
            return ActionMutation(
                result={"redemption_id": redemption_id, "prize_id": body.prize_id, "cost": cost},
                details={"name": prize.name},
            )

        return await commit_action(username=username, body=body, action_type="prize.redeem", mutator=mutate)

    @app.post("/actions/import")
    async def import_data(body: ImportRequest, username: Annotated[str, Depends(current_user_dep)]) -> ActionResponse:
        store = get_store(username)

        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            store.replace_state_for_import(s, body.data)
            return ActionMutation(result={"imported": True})

        return await commit_action(
            username=username, body=body, action_type="data.import", mutator=mutate, snapshot_reason="before_import"
        )

    @app.post("/actions/reset")
    async def reset_data(body: ResetRequest, username: Annotated[str, Depends(current_user_dep)]) -> ActionResponse:
        store = get_store(username)

        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            store.replace_state_for_reset(s)
            return ActionMutation(result={"reset": True})

        return await commit_action(
            username=username, body=body, action_type="data.reset", mutator=mutate, snapshot_reason="before_reset"
        )

    @app.post("/casino/slots/spin")
    async def slots_spin(body: SlotsSpinRequest, username: Annotated[str, Depends(current_user_dep)]) -> ActionResponse:
        rng = SecretsRandom()

        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            _require_credits(s, body.wager_credits)
            settlement = spin_slots(body.wager_credits, rng)
            balance = _balance(s)
            balance.credits -= body.wager_credits
            balance.tokens += settlement.payout_tokens
            result = settlement.outcome | {"payout_tokens": settlement.payout_tokens}
            return ActionMutation(
                result=result,
                details={"wager_credits": body.wager_credits},
                game_event={
                    "game": "slots",
                    "wager_credits": body.wager_credits,
                    "payout_tokens": settlement.payout_tokens,
                    "outcome": settlement.outcome,
                },
                rng_version=RNG_VERSION,
            )

        return await commit_action(username=username, body=body, action_type="casino.slots.spin", mutator=mutate)

    @app.post("/casino/roulette/spin")
    async def roulette_spin(
        body: RouletteSpinRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        rng = SecretsRandom()

        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            _require_credits(s, body.wager_credits)
            try:
                settlement = spin_roulette(body.wager_credits, body.bet_type, body.bet_number, rng)
            except ValueError as e:
                raise ActionRejectedError("roulette_bet", str(e)) from e
            balance = _balance(s)
            balance.credits -= body.wager_credits
            balance.tokens += settlement.payout_tokens
            result = settlement.outcome | {"payout_tokens": settlement.payout_tokens}
            return ActionMutation(
                result=result,
                details={"wager_credits": body.wager_credits, "bet_type": body.bet_type, "bet_number": body.bet_number},
                game_event={
                    "game": "roulette",
                    "wager_credits": body.wager_credits,
                    "payout_tokens": settlement.payout_tokens,
                    "outcome": settlement.outcome,
                },
                rng_version=RNG_VERSION,
            )

        return await commit_action(username=username, body=body, action_type="casino.roulette.spin", mutator=mutate)

    @app.post("/casino/blackjack/deal")
    async def blackjack_deal(
        body: BlackjackDealRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        rng = SecretsRandom()

        def mutate(s: Session, now_ms: int) -> ActionMutation:
            _require_credits(s, body.wager_credits)
            shoe = make_shoe(rng)
            p1, shoe = draw_cards(shoe, 1)
            d1, shoe = draw_cards(shoe, 1)
            p2, shoe = draw_cards(shoe, 1)
            d2, shoe = draw_cards(shoe, 1)
            player = [*p1, *p2]
            dealer = [*d1, *d2]
            balance = _balance(s)
            balance.credits -= body.wager_credits
            credits_after_wager = balance.credits
            tokens_before_settle = balance.tokens
            hand_id = f"bj-{uuid.uuid4()}"
            status = "playing"
            settlement = None
            if is_blackjack(player) or is_blackjack(dealer):
                settlement = settle_blackjack(player, dealer, body.wager_credits)
                if settlement.payout_tokens:
                    balance.tokens += settlement.payout_tokens
                status = "done"
            row = BlackjackHandRow(
                id=hand_id,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
                status=status,
                wager_credits=body.wager_credits,
                current_wager_credits=body.wager_credits,
                credits_before=credits_after_wager + body.wager_credits,
                tokens_before=tokens_before_settle,
                shoe_json=json.dumps(shoe, separators=(",", ":")),
                player_json=json.dumps(player, separators=(",", ":")),
                dealer_json=json.dumps(dealer, separators=(",", ":")),
                result_json=json.dumps(settlement.outcome, separators=(",", ":")) if settlement else None,
            )
            s.add(row)
            result = public_blackjack_state(
                hand_id=hand_id,
                status=status,
                player=player,
                dealer=dealer,
                current_wager=body.wager_credits,
                settlement=settlement,
            )
            game_event = (
                {
                    "game": "blackjack",
                    "wager_credits": body.wager_credits,
                    "payout_tokens": settlement.payout_tokens,
                    "outcome": settlement.outcome | {"initial_wager": body.wager_credits, "doubled": False},
                }
                if settlement
                else None
            )
            return ActionMutation(
                result=result,
                details={"hand_id": hand_id, "wager_credits": body.wager_credits},
                game_event=game_event,
                rng_version=RNG_VERSION,
            )

        return await commit_action(username=username, body=body, action_type="blackjack.deal", mutator=mutate)

    @app.post("/casino/blackjack/hit")
    async def blackjack_hit(
        body: BlackjackHandRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        rng = SecretsRandom()

        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            return _mutate_blackjack_step(s, body.hand_id, "hit", rng)

        return await commit_action(username=username, body=body, action_type="blackjack.hit", mutator=mutate)

    @app.post("/casino/blackjack/stand")
    async def blackjack_stand(
        body: BlackjackHandRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        rng = SecretsRandom()

        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            return _mutate_blackjack_step(s, body.hand_id, "stand", rng)

        return await commit_action(username=username, body=body, action_type="blackjack.stand", mutator=mutate)

    @app.post("/casino/blackjack/double")
    async def blackjack_double(
        body: BlackjackHandRequest, username: Annotated[str, Depends(current_user_dep)]
    ) -> ActionResponse:
        rng = SecretsRandom()

        def mutate(s: Session, _now_ms: int) -> ActionMutation:
            return _mutate_blackjack_step(s, body.hand_id, "double", rng)

        return await commit_action(username=username, body=body, action_type="blackjack.double", mutator=mutate)

    @app.websocket("/ws")
    async def websocket_state_changed(ws: WebSocket) -> None:
        """Thin push channel for cross-tab consistency.

        On connect: server sends one `{"type":"state_changed"}` so the tab
        knows to do an initial `GET /state` (the WS isn't strictly required
        — the tab could just hit /state on mount — but having the bootstrap
        ping unifies the refetch trigger).

        Subsequently: every successful server action triggers a fan-out to
        every connected tab of the same user (including the originator;
        idempotent refetch is harmless and keeps the optimistic UI honest).
        """
        # Auth: read the HMAC-signed session cookie from the WS upgrade request.
        if oidc is not None:
            casino_session = ws.cookies.get("casino_session")
            if not casino_session:
                await ws.close(code=4001, reason="not authenticated")
                return
            username = decode_session_token(casino_session, oidc.session_secret)
            if username is None:
                await ws.close(code=4001, reason="session invalid or expired")
                return
        else:
            username = "default"

        await ws.accept()
        ws_manager.add(username, ws)
        logger.info("ws connected: user=%s", username)
        try:
            await ws.send_json({"type": "state_changed"})
        except Exception:
            ws_manager.remove(username, ws)
            return

        try:
            while True:
                # Drain any client messages (we don't expect any) so the
                # connection stays open until the client or server closes it.
                try:
                    await ws.receive_text()
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
        finally:
            ws_manager.remove(username, ws)
            logger.info("ws disconnected: user=%s", username)

    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
    else:
        logger.warning("frontend dist dir %s not found — serving API only", frontend_dist)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = Settings()
    logger.info("study casino listening on %s:%d, data_dir=%s", settings.host, settings.port, settings.data_dir)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
