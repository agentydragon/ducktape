"""Python-owned monthly orchestration with one ordered batch policy response per month.

`ActionSession` drives one `World` per selected path and records, between steps, the
summary and trace its `Finished` promises; the world keeps none of that history.
"""

from finance.augur.sim import capture, results
from finance.augur.sim.actions import Action, DecisionActions
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.books import AccountRef, TaxAccrual, TaxPaymentOutcome, TaxSettlementOutcome
from finance.augur.sim.observations import Decision, Observation
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.validation import validate
from finance.augur.sim.world import Capture, World, acting_agent, validate_actor


class _Delegate(EconomicAgent):
    """The batch caller's stand-in on each world: it hands over the actions the caller submitted."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(agent_id)
        self.pending: list[Action] = []

    def decide(self, observation: Observation) -> list[Action]:
        return self.pending


class _Session:
    """Own the selected worlds, the shared clock and the batch routing envelope."""

    def __init__(
        self, run: CompiledRun, actor: str | None, rollout_ids: list[int], *, capture: Capture, configured: bool = False
    ) -> None:
        if not isinstance(run, CompiledRun):
            raise TypeError("execution requires a CompiledRun, not serialized input")
        if (
            not rollout_ids
            or len(set(rollout_ids)) != len(rollout_ids)
            or any(
                not isinstance(id_, int) or isinstance(id_, bool) or not 0 <= id_ < run.rollout_count
                for id_ in rollout_ids
            )
        ):
            raise ValueError("selected rollout IDs must be unique, nonempty and in range")
        if capture not in ("summary", "dense", "forensic"):
            raise ValueError("capture must be summary, dense or forensic")
        if not configured:
            if actor is None:
                raise ValueError("action sessions require an actor")
            validate_actor(run, actor)
        validate(run)
        self.run = run
        self.actor = actor
        self.configured = configured
        self.capture = capture
        self.month = 0
        self.started = False
        self.closed = False
        self.paths = {rollout_id: World(run, rollout_id) for rollout_id in rollout_ids}
        self.delegates: dict[int, _Delegate] = {}
        if actor is not None and not configured:
            for rollout_id, world in self.paths.items():
                delegate = _Delegate(actor)
                world._track(delegate)
                self.delegates[rollout_id] = delegate

    def active(self) -> dict[int, World]:
        return {id_: path for id_, path in self.paths.items() if not path.finished}

    def is_finished(self) -> bool:
        return self.started and all(path.finished for path in self.paths.values())

    def _check_open(self) -> None:
        if self.closed or self.is_finished():
            raise ValueError("session is finished, aborted or closed")

    def start(self) -> None:
        self._check_open()
        if self.started:
            raise ValueError("invalid session lifecycle state: already started")
        self.started = True
        for path in self.active().values():
            path.start()

    def observe(self, rollout_id: int, actor: str) -> Observation:
        return self.paths[rollout_id].observe(actor)

    def begin_actions(self, responses: list[DecisionActions]) -> None:
        """Validate the complete routing envelope before executing any action."""
        self._check_open()
        if not self.started:
            raise ValueError("invalid session lifecycle state: not started")
        if not all(isinstance(response, DecisionActions) for response in responses):
            raise TypeError("responses must be DecisionActions")
        if any(
            not isinstance(key, int) or isinstance(key, bool)
            for response in responses
            for key in (response.rollout_id, response.month)
        ):
            raise TypeError("response rollout ID and month must be integers")
        keys = [(response.rollout_id, response.month) for response in responses]
        if len(set(keys)) != len(keys) or set(keys) != {(id_, self.month) for id_ in self.active()}:
            raise ValueError("responses must name each active path/month exactly once")
        for response in responses:
            self.paths[response.rollout_id].check_claims(response.actions)
        for response in responses:
            self.paths[response.rollout_id].begin_actions(response.actions)

    def step(self, responses: list[DecisionActions]) -> None:
        """Hand each path its submitted actions and step it; the caller records before `open_month`."""
        for response in responses:
            self.delegates[response.rollout_id].pending = response.actions
            self.paths[response.rollout_id].step()
        self.month += 1

    def apply(self, rollout_id: int, action: Action) -> results.Receipt:
        """The configured runner's per-action execution; the action names its own agent."""
        return self.paths[rollout_id].execute(acting_agent(action), action)

    def close_month(self) -> None:
        for path in self.active().values():
            path.close_month()
        self.month += 1

    def open_month(self) -> None:
        for path in self.active().values():
            path.open_month()

    def close(self) -> None:
        self.closed = True
        self.paths.clear()


class _Record:
    """What `ActionSession` promises per path, read from world state after every step."""

    def __init__(self, world: World, actor: str, mode: Capture) -> None:
        self.world = world
        self.actor = actor
        self.mode = mode
        self.cash = [
            results.CashSeries(account=account.account, values=[])
            for account in world.scenario.accounts
            if account.account.agent_id == actor
        ]
        self.holdings: dict[tuple[AccountRef, str], list[int]] = {}
        self.bond_terms = [bond for bond in world.bonds.terms if bond.agent_id == actor]
        self.bonds = [
            results.BondSeries(
                account=AccountRef(agent_id=bond.agent_id, account_id=bond.account_id), bond_id=bond.bond_id, values=[]
            )
            for bond in self.bond_terms
        ]
        self.payments: list[results.Payment] = []
        self.receipts: list[results.Receipt] = []
        self.tax_accruals: list[TaxAccrual] = []
        self.tax_payments: list[TaxPaymentOutcome] = []
        self.tax_settlements: list[TaxSettlementOutcome] = []
        self.financial = capture.FinancialCapture(world, capture=mode) if mode != "summary" else None
        self._series()

    def _series(self) -> None:
        world = self.world
        mark = world.mark_month
        for series in self.cash:
            series.values.append(world.accounting.ledger.balance(series.account))
        for bond, series in zip(self.bond_terms, self.bonds, strict=True):
            value = world.bonds.held_principal(bond, world.month, mark)
            series.values.append(0 if value is None else value)
        keys = {
            (AccountRef(agent_id=lot.spec.agent_id, account_id=lot.spec.account_id), lot.spec.asset_id)
            for lot in world.holdings.lots
            if lot.spec.agent_id == self.actor
        }
        keys.update(
            (AccountRef(agent_id=row.owner_agent_id, account_id=row.account_id), row.asset_id)
            for row in world.managed.marks.values()
            if row.owner_agent_id == self.actor
        )
        for key in keys:
            self.holdings.setdefault(key, [0] * world.month)
        for (account, asset), values in self.holdings.items():
            values.append(world.holding_value(self.actor, mark, account.account_id, asset))

    def record(self) -> None:
        """Call after the world closed a month and before the next one opens."""
        world = self.world
        self.payments.extend(world.payments)
        self.tax_accruals.extend(world.accounting.tax_accruals)
        self.tax_payments.extend(world.accounting.tax_payments)
        self.tax_settlements.extend(world.accounting.tax_settlements)
        if self.mode != "summary":
            self.receipts.extend(world.previous_receipts)
        if self.financial is not None:
            self.financial.record()
        self._series()

    def rollout(self) -> results.Rollout:
        world = self.world
        summary = results.Summary(
            actor_id=self.actor,
            cash=self.cash,
            public_holdings=[
                results.HoldingSeries(account=account, asset_id=asset, values=values)
                for (account, asset), values in sorted(
                    self.holdings.items(), key=lambda item: (item[0][0].agent_id, item[0][0].account_id, item[0][1])
                )
            ],
            bond_principal=self.bonds,
            payments=self.payments,
            unpaid_claims=world.unpaid_claims(self.actor),
            tax_accruals=self.tax_accruals,
            tax_payments=self.tax_payments,
            tax_settlements=self.tax_settlements,
            ending_book=world.book(),
            ending_mark_month=world.mark_month,
            last_receipts=list(world.previous_receipts),
        )
        trace = None
        if self.financial is not None:
            financial = self.financial.financial()
            if financial is None:
                raise RuntimeError("detailed capture requires financial output")
            trace = results.Trace(
                events=capture.event_log(financial),
                books=financial.months,
                journal=financial.journal,
                bond_cashflows=financial.bond_cashflows,
                distributions=financial.distributions,
                receipts=self.receipts,
            )
        return results.Rollout(rollout_id=world.rollout_id, summary=summary, trace=trace, stop=world.stop)


class ActionSession:
    """One household's batch session; only the caller invokes policy code.

    Submit one keyed ordered response per current path. Rejection stops that path,
    preserving earlier effects; no retries or engine-selected rescue actions occur.
    """

    def __init__(self, run: CompiledRun, actor: str, rollout_ids: list[int], *, capture: Capture = "forensic") -> None:
        self._session = _Session(run, actor, rollout_ids, capture=capture)
        self._records = {id_: _Record(world, actor, capture) for id_, world in self._session.paths.items()}

    def _result(self) -> list[Decision] | results.Finished:
        session = self._session
        if session.is_finished():
            return results.Finished(rollouts=[record.rollout() for record in self._records.values()])
        if session.actor is None:
            raise RuntimeError("action sessions require an actor")
        return [Decision(id_, session.observe(id_, session.actor)) for id_ in session.active()]

    def start(self) -> list[Decision] | results.Finished:
        try:
            self._session.start()
            return self._result()
        except BaseException:
            self.close()
            raise

    def advance(self, responses: list[DecisionActions]) -> list[Decision] | results.Finished:
        try:
            self._session.begin_actions(responses)
            self._session.step(responses)
            for response in responses:
                self._records[response.rollout_id].record()
            self._session.open_month()
            return self._result()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._session.close()
