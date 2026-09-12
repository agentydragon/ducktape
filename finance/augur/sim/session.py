"""Python-owned monthly orchestration with one ordered batch policy response per month.

`ActionSession` owns one `World` per selected path, feeds each a delegate household
carrying the caller's submitted actions, and records between steps the summary and
trace its `Finished` promises; the world keeps none of that history.
"""

from finance.augur.sim import capture, results
from finance.augur.sim.actions import Action, DecisionActions
from finance.augur.sim.agent import EconomicAgent, assemble
from finance.augur.sim.books import AccountRef, TaxAccrual, TaxPaymentOutcome, TaxSettlementOutcome
from finance.augur.sim.ids import AgentId
from finance.augur.sim.observations import Decision, Observation
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.validation import validate
from finance.augur.sim.world import Capture, World, validate_actor


class _Delegate(EconomicAgent):
    """The batch caller's stand-in on each world: it hands over the actions the caller submitted."""

    def __init__(self, agent_id: AgentId) -> None:
        super().__init__(agent_id)
        self.pending: list[Action] = []

    def decide(self, observation: Observation) -> list[Action]:
        return self.pending


class _Record:
    """What `ActionSession` promises per path, read from world state after every step."""

    def __init__(self, world: World, actor: str, mode: Capture) -> None:
        self.world = world
        self.actor = actor
        self.mode = mode
        self.cash = [
            results.CashSeries(account=account, values=[])
            for account in world.accounting.declared
            if account.agent_id == actor
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
        validate_actor(run, actor)
        validate(run)
        self._actor = AgentId(actor)
        self._month = 0
        self._started = False
        self._closed = False
        self._paths = {rollout_id: World.from_run(run, rollout_id) for rollout_id in rollout_ids}
        self._delegates: dict[int, _Delegate] = {}
        for rollout_id, world in self._paths.items():
            delegate = _Delegate(self._actor)
            world._track(delegate)
            self._delegates[rollout_id] = delegate
        self._records = {id_: _Record(world, actor, capture) for id_, world in self._paths.items()}

    def _active(self) -> dict[int, World]:
        return {id_: path for id_, path in self._paths.items() if not path.finished}

    def _is_finished(self) -> bool:
        return self._started and all(path.finished for path in self._paths.values())

    def _check_open(self) -> None:
        if self._closed or self._is_finished():
            raise ValueError("session is finished, aborted or closed")

    def _result(self) -> list[Decision] | results.Finished:
        if self._is_finished():
            return results.Finished(rollouts=[record.rollout() for record in self._records.values()])
        return [Decision(id_, assemble(self._actor, self._month, self._delegates[id_].mail)) for id_ in self._active()]

    def start(self) -> list[Decision] | results.Finished:
        try:
            self._check_open()
            if self._started:
                raise ValueError("invalid session lifecycle state: already started")
            self._started = True
            for path in self._paths.values():
                path.start()
            return self._result()
        except BaseException:
            self.close()
            raise

    def _check_envelope(self, responses: list[DecisionActions]) -> None:
        """Validate the complete routing envelope before executing any action."""
        self._check_open()
        if not self._started:
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
        if len(set(keys)) != len(keys) or set(keys) != {(id_, self._month) for id_ in self._active()}:
            raise ValueError("responses must name each active path/month exactly once")
        for response in responses:
            self._paths[response.rollout_id].check_claims(response.actions)

    def advance(self, responses: list[DecisionActions]) -> list[Decision] | results.Finished:
        try:
            self._check_envelope(responses)
            for response in responses:
                self._delegates[response.rollout_id].pending = response.actions
                self._paths[response.rollout_id].step()
                self._records[response.rollout_id].record()
            self._month += 1
            for path in self._active().values():
                path.open_month()
            return self._result()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._closed = True
        self._paths.clear()
