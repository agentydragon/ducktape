from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from rich import box
from rich.console import Group
from rich.table import Table
from textual.app import App, ComposeResult
from textual.events import Key
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

from . import store as probe_store
from .core import FAMILY_PRIORITY, FATAL_CODES, ErrorCode, Family, ModelProbe, ProbeRun, build_cell, family_of

if TYPE_CHECKING:
    from .main import ProbeResult

logger = logging.getLogger(__name__)


class ProbeTUI(App):
    """Interactive TUI for the OpenAI probe."""

    CSS = """
    Screen { align: center middle; }
    #body { width: 100%; height: auto; }
    """

    BINDINGS: ClassVar = [
        ("f", "toggle_fatal", "Toggle fatal models"),
        ("tab", "next_family", "Next family"),
        ("q", "quit", "Quit"),
    ]

    show_fatal = reactive(False)
    family_idx = reactive(0)

    def __init__(
        self,
        *,
        out_q: asyncio.Queue[tuple[str, str, ProbeResult | None]],
        total_runners: int,
        filtered: list[str],
        repeats: int,
        initial_show_fatal: bool = False,
    ) -> None:
        super().__init__()
        self.out_q = out_q
        self.total_runners = total_runners
        self.filtered = filtered
        self.repeats = repeats
        self.show_fatal = initial_show_fatal
        present = {family_of(mid) for mid in filtered}
        ordered: list[Family] = [fam for fam in FAMILY_PRIORITY if fam in present]
        if Family.OTHER in present and Family.OTHER not in ordered:
            ordered.append(Family.OTHER)
        self.family_choices: list[Family | None] = [None, *ordered]  # None = ALL
        self.family_idx = 0
        self.acc: dict[str, dict[str, list[Any]]] = {mid: {"responses": [], "chat": []} for mid in filtered}
        self.done_flags: dict[str, dict[str, bool]] = {mid: {"responses": False, "chat": False} for mid in filtered}
        self.finalized: set[str] = set()
        self.completed_models = 0
        self.finished = 0
        self.used_codes: dict[str, str] = {}
        self.fatal_by_mid: dict[str, bool] = dict.fromkeys(filtered, False)
        self.oks: list[ModelProbe] = []
        self.fails: list[ModelProbe] = []

    def compose(self) -> ComposeResult:  # type: ignore[override]
        yield Header(show_clock=True)
        self.body = Static(id="body")
        yield self.body
        yield Footer()

    async def on_mount(self) -> None:
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._render_view()

    async def _reader_loop(self) -> None:
        while self.finished < self.total_runners:
            kind, mid, res = await self.out_q.get()
            if res is None:
                self.done_flags[mid][kind] = True
                if mid not in self.finalized and all(self.done_flags[mid].values()):
                    self.completed_models += 1
                    probe = ModelProbe(
                        model_id=mid,
                        responses=ProbeRun(model_id=mid, calls=self.acc[mid]["responses"]),
                        chat=ProbeRun(model_id=mid, calls=self.acc[mid]["chat"]),
                    )
                    (self.oks if probe.any_ok else self.fails).append(probe)
                    self.finalized.add(mid)
                self.finished += 1
            else:
                self.acc[mid][kind].append(res)
                try:
                    probe_store.persist_result(res)
                except Exception as e:
                    logger.debug("persist_result failed: %s", e)
                try:
                    # Best-effort DB write; UI shouldn't fail if DB is unavailable
                    await probe_store.write_probe_result(res)
                except Exception as e:
                    logger.debug("write_probe_result failed: %s", e)
                if not res.ok:
                    classification = res.error_classification
                    if classification:
                        code, desc = classification
                        if code != ErrorCode.OTHER:
                            self.used_codes.setdefault(code.value, desc)
                        if code in FATAL_CODES:
                            self.fatal_by_mid[mid] = True
            self._render_view()
        self._render_view(final=True)

    def _family_label(self, fam: Family | None) -> str:
        if fam is None:
            return "ALL"
        return str(fam.value)

    @property
    def current_family(self) -> Family | None:
        return self.family_choices[self.family_idx]

    def _filter_mid(self, mid: str) -> bool:
        fam = family_of(mid)
        if self.current_family is not None and fam != self.current_family:
            return False
        if self.show_fatal:
            return self.fatal_by_mid.get(mid, False)
        return True

    def on_key(self, event: Key) -> None:
        if event.key == "q":
            self.exit()
        elif event.key == "f":
            self.show_fatal = not self.show_fatal
            self._render_view()
        elif event.key == "tab":
            self.family_idx = (self.family_idx + 1) % len(self.family_choices)
            self._render_view()

    def _make_results_table(self, *, title: str, header_style: str, model_ratio: int) -> Table:
        table = Table(show_header=True, header_style=header_style, title=title, expand=True, box=box.SIMPLE)
        table.add_column("Model", overflow="ellipsis", no_wrap=True, ratio=model_ratio, header_style="bold")
        table.add_column("Responses", overflow="ellipsis", no_wrap=True, ratio=2)
        table.add_column("Chat", overflow="ellipsis", no_wrap=True, ratio=2)
        return table

    def _iter_with_break(self, rows: list[Any], key_fn) -> list[tuple[Any, bool]]:
        out = []
        for i, r in enumerate(rows):
            curr = key_fn(r)
            nxt = key_fn(rows[i + 1]) if i + 1 < len(rows) else None
            out.append((r, nxt is not None and nxt != curr))
        return out

    def _partials_sort_key(self, kind: str):
        def _inner(item: tuple[str, str, str]) -> tuple[int, str]:
            return (FAMILY_PRIORITY.index(Family(item[0])) if Family(item[0]) in FAMILY_PRIORITY else 999, item[0])

        return _inner

    def _render_view(self, final: bool = False) -> None:
        # Build tables for the TUI view, applying fatal filter
        new_table = self._make_results_table(
            title=f"OpenAI API Probe — Families: {', '.join([self._family_label(f) for f in self.family_choices])} (current={self._family_label(self.current_family)})",
            header_style="bold green",
            model_ratio=3,
        )

        def render_row(table: Table, r: ModelProbe, end_section: bool):
            rcell, rcode, rdesc = build_cell(r.responses.calls)
            ccell, ccode, cdesc = build_cell(r.chat.calls)
            if rcode and rdesc:
                self.used_codes.setdefault(rcode.value if isinstance(rcode, ErrorCode) else str(rcode), rdesc)
            if ccode and cdesc:
                self.used_codes.setdefault(ccode.value if isinstance(ccode, ErrorCode) else str(ccode), cdesc)
            table.add_row(r.model_id, rcell, ccell, end_section=end_section)

        finalized_sorted: list[Any] = sorted(self.oks, key=lambda r: (family_of(r.model_id).value, r.model_id))
        for i, r in enumerate(finalized_sorted):
            nxt = family_of(finalized_sorted[i + 1].model_id).value if i + 1 < len(finalized_sorted) else None
            curr = family_of(r.model_id).value
            end_section = nxt is not None and nxt != curr
            if self._filter_mid(r.model_id):
                render_row(new_table, r, end_section)

        resp_table = self._make_results_table(
            title="Responses (in-progress)", header_style="bold yellow", model_ratio=3
        )
        chat_table = self._make_results_table(title="Chat (in-progress)", header_style="bold yellow", model_ratio=3)

        partials_resp: list[tuple[str, str, str]] = []
        partials_chat: list[tuple[str, str, str]] = []
        for mid2 in sorted(self.filtered, key=lambda m: (family_of(m).value, m)):
            if not self._filter_mid(mid2):
                continue
            rc2, _, _ = build_cell(self.acc[mid2]["responses"])
            cc2, _, _ = build_cell(self.acc[mid2]["chat"])
            if rc2 != "[yellow]waiting…[/yellow]" or cc2 != "[yellow]waiting…[/yellow]":
                partials_resp.append((family_of(mid2).value, mid2, rc2))
                partials_chat.append((family_of(mid2).value, mid2, cc2))

        partials_resp.sort(key=self._partials_sort_key("responses"))
        partials_chat.sort(key=self._partials_sort_key("chat"))
        for (mid4, rc, cc), end_section in self._iter_with_break(partials_resp, lambda x: family_of(x[0]).value):
            resp_table.add_row(mid4, rc, cc, end_section=end_section)
        for (mid5, rc, cc), end_section in self._iter_with_break(partials_chat, lambda x: family_of(x[0]).value):
            chat_table.add_row(mid5, rc, cc, end_section=end_section)

        renderables = [new_table, resp_table, chat_table]
        if self.current_family is None or self.current_family == Family.OTHER:
            others_all = sorted(
                [mid6 for mid6 in self.filtered if family_of(mid6) == Family.OTHER and self._filter_mid(mid6)]
            )
            others_table = self._make_results_table(
                title="Other models (unclassified)", header_style="bold magenta", model_ratio=3
            )
            for mid7 in others_all:
                rc, _, _ = build_cell(self.acc[mid7]["responses"])
                cc, _, _ = build_cell(self.acc[mid7]["chat"])
                others_table.add_row(mid7, rc, cc)
            renderables.append(others_table)

        if final or self.fails:
            if self.fails:
                self.fails.sort(key=lambda r: (family_of(r.model_id).value, r.model_id))
                fail_table = Table(
                    show_header=True, header_style="bold red", title="Failures", expand=True, box=box.SIMPLE
                )
                fail_table.add_column("Model", overflow="ellipsis", no_wrap=True, ratio=3, header_style="bold")
                fail_table.add_column("Responses", overflow="ellipsis", no_wrap=True, ratio=2)
                fail_table.add_column("Chat", overflow="ellipsis", no_wrap=True, ratio=2)
                for i, r in enumerate(self.fails):
                    if not self._filter_mid(r.model_id):
                        continue
                    rcell, rcode, rdesc = build_cell(r.responses.calls)
                    ccell, ccode, cdesc = build_cell(r.chat.calls)
                    if rcode and rdesc:
                        self.used_codes.setdefault(rcode.value if isinstance(rcode, ErrorCode) else str(rcode), rdesc)
                    if ccode and cdesc:
                        self.used_codes.setdefault(ccode.value if isinstance(ccode, ErrorCode) else str(ccode), cdesc)
                    nxt = family_of(self.fails[i + 1].model_id).value if i + 1 < len(self.fails) else None
                    curr = family_of(r.model_id).value
                    end_section = nxt is not None and nxt != curr
                    fail_table.add_row(r.model_id, rcell, ccell, end_section=end_section)
                renderables.append(fail_table)
            if self.used_codes:
                legend = Table(
                    show_header=True,
                    header_style="bold cyan",
                    title="Legend (error codes)",
                    expand=True,
                    box=box.SIMPLE,
                )
                legend.add_column("Code")
                legend.add_column("Meaning", overflow="fold")
                for code_str, meaning in sorted(self.used_codes.items()):
                    legend.add_row(code_str, meaning)
                renderables.append(legend)

        self.body.update(Group(*renderables))


async def consume_stream_textual(
    out_q: asyncio.Queue[tuple[str, str, ProbeResult | None]],
    total_runners: int,
    filtered: list[str],
    repeats: int,
    *,
    show_fatal: bool = False,
) -> None:
    app = ProbeTUI(
        out_q=out_q, total_runners=total_runners, filtered=filtered, repeats=repeats, initial_show_fatal=show_fatal
    )
    await app.run_async()
