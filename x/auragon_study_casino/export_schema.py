"""Export the Study Casino API OpenAPI schema to stdout.

Schema-only FastAPI app: every route declares the same request/response models
as the real backend in `app.py`, but the handler bodies raise — `app.openapi()`
only consults the route signatures. The frontend codegen target
`//x/auragon_study_casino/frontend/lib:schema_zod` consumes this JSON to emit
Zod schemas the frontend parses at the fetch boundary.

Keeping this separate from `app.py` (which creates a real `SqlStore` on the
configured Postgres URL) means schema export has zero runtime dependencies —
no DB, no settings, no auth secrets.
"""

from __future__ import annotations

import json
from typing import NoReturn

from fastapi import FastAPI

from x.auragon_study_casino.actions import (
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
from x.auragon_study_casino.events import GameEventRead, LedgerEventRead
from x.auragon_study_casino.state import (
    AdminUsersResponse,
    HealthResponse,
    MeResponse,
    StateDump,
    WsStateChangedMessage,
)
from x.auragon_study_casino.stats import CasinoStats


def _stub() -> NoReturn:
    raise RuntimeError("schema-only route")


def create_schema_app() -> FastAPI:
    app = FastAPI(title="Study Casino")

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return _stub()

    @app.get("/me", response_model=MeResponse)
    def me() -> MeResponse:
        return _stub()

    @app.get("/state", response_model=StateDump)
    def get_state() -> StateDump:
        return _stub()

    @app.get("/admin/users", response_model=AdminUsersResponse)
    def admin_users() -> AdminUsersResponse:
        return _stub()

    @app.get("/admin/state", response_model=StateDump)
    def admin_state() -> StateDump:
        return _stub()

    @app.get("/game-events", response_model=list[GameEventRead])
    def list_game_events() -> list[GameEventRead]:
        return _stub()

    @app.get("/ledger-events", response_model=list[LedgerEventRead])
    def list_ledger_events() -> list[LedgerEventRead]:
        return _stub()

    @app.get("/casino/stats", response_model=CasinoStats)
    def casino_stats() -> CasinoStats:
        return _stub()

    @app.get("/admin/casino/stats", response_model=CasinoStats)
    def admin_casino_stats() -> CasinoStats:
        return _stub()

    @app.post("/actions/session/complete", response_model=ActionResponse)
    def session_complete(_: SessionCompleteRequest) -> ActionResponse:
        return _stub()

    @app.post("/actions/session/add-past", response_model=ActionResponse)
    def session_add_past(_: AddPastSessionRequest) -> ActionResponse:
        return _stub()

    @app.post("/actions/session/edit", response_model=ActionResponse)
    def session_edit(_: EditSessionRequest) -> ActionResponse:
        return _stub()

    @app.post("/actions/session/delete", response_model=ActionResponse)
    def session_delete(_: DeleteSessionRequest) -> ActionResponse:
        return _stub()

    @app.post("/actions/convert", response_model=ActionResponse)
    def convert(_: ConvertRequest) -> ActionResponse:
        return _stub()

    @app.post("/actions/prize/create", response_model=ActionResponse)
    def prize_create(_: PrizeCreateRequest) -> ActionResponse:
        return _stub()

    @app.post("/actions/prize/delete", response_model=ActionResponse)
    def prize_delete(_: PrizeDeleteRequest) -> ActionResponse:
        return _stub()

    @app.post("/actions/prize/redeem", response_model=ActionResponse)
    def prize_redeem(_: PrizeRedeemRequest) -> ActionResponse:
        return _stub()

    @app.post("/actions/import", response_model=ActionResponse)
    def import_state(_: ImportRequest) -> ActionResponse:
        return _stub()

    @app.post("/actions/reset", response_model=ActionResponse)
    def reset_state(_: ResetRequest) -> ActionResponse:
        return _stub()

    @app.post("/casino/slots/spin", response_model=ActionResponse)
    def slots_spin(_: SlotsSpinRequest) -> ActionResponse:
        return _stub()

    @app.post("/casino/roulette/spin", response_model=ActionResponse)
    def roulette_spin(_: RouletteSpinRequest) -> ActionResponse:
        return _stub()

    @app.post("/casino/blackjack/deal", response_model=ActionResponse)
    def blackjack_deal(_: BlackjackDealRequest) -> ActionResponse:
        return _stub()

    @app.post("/casino/blackjack/hit", response_model=ActionResponse)
    def blackjack_hit(_: BlackjackHandRequest) -> ActionResponse:
        return _stub()

    @app.post("/casino/blackjack/stand", response_model=ActionResponse)
    def blackjack_stand(_: BlackjackHandRequest) -> ActionResponse:
        return _stub()

    @app.post("/casino/blackjack/double", response_model=ActionResponse)
    def blackjack_double(_: BlackjackHandRequest) -> ActionResponse:
        return _stub()

    # /ws messages are not HTTP endpoints — FastAPI doesn't emit them into
    # the OpenAPI schema. Declare a schema-only POST so the message body type
    # lands in `components.schemas` for the frontend.
    @app.post("/_ws_state_changed", response_model=WsStateChangedMessage, include_in_schema=True)
    def _ws_state_changed(_: WsStateChangedMessage) -> WsStateChangedMessage:
        return _stub()

    return app


def main() -> None:
    print(json.dumps(create_schema_app().openapi(), indent=2))


if __name__ == "__main__":
    main()
