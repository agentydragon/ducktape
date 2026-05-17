from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar, overload

import numpy as np

from augur.core.accounting import (
    BalanceSnapshot,
    ChartAccount,
    ChartAccountRole,
    JournalEntry,
    JournalEntryType,
    LiabilityState,
    LotAssetClass,
    LotDisposition,
    Posting,
    PostingSide,
    TaxLot,
)
from augur.core.market_bundle import (
    MarketBundle,
    MarketBundleProvider,
    RequiredMarketKeys,
    SimpleMarketBundleProvider,
    sample_market_bundle_for_request,
)
from augur.core.provenance import (
    ProjectionRun,
    policy_program_set_id,
    projection_run_id,
    projection_trajectory_id,
    scenario_input_id,
)
from augur.core.scenario_engine import ScenarioRunArrays, report_metric_array, run_scenario_vectorized
from augur.core.scenario_set import (
    AccountingDetail,
    ActorRole,
    CryptoAssetPosition,
    Effect,
    EventType,
    ExogenousPathIdentity,
    MarketObservation,
    PartnerEquityAccrualPolicy,
    PolicyDecision,
    PrivateEquityPosition,
    ProjectionTrajectoryIdentity,
    RentalMode,
    ReportMetric,
    ReportSpec,
    RolloutStatus,
    RolloutStatusSummary,
    Scenario,
    ScenarioAcceptedSummary,
    ScenarioResult,
    ScenarioSet,
    ScenarioSetRunResponse,
)

EffectT = TypeVar("EffectT")
AccountingDetailT = TypeVar("AccountingDetailT")
DecisionT = TypeVar("DecisionT")
ObservationT = TypeVar("ObservationT")


class ScenarioValidationError(ValueError):
    """Raised when a typed scenario is internally inconsistent."""


@dataclass(frozen=True)
class RolloutDetail:
    scenario_run: ScenarioRun
    rollout_index: int

    def series(self, metric: ReportMetric) -> np.ndarray:
        return self.scenario_run.series(metric, rollout=self.rollout_index)

    def terminal(self, metric: ReportMetric) -> float:
        return float(self.scenario_run.terminal(metric, rollout=self.rollout_index))

    @overload
    def effects(self, effect_type: type[EffectT]) -> tuple[EffectT, ...]: ...

    @overload
    def effects(self, effect_type: None = None) -> tuple[Effect, ...]: ...

    def effects(self, effect_type: type[Any] | None = None) -> tuple[Any, ...]:
        return self.scenario_run.effects(effect_type, rollout=self.rollout_index)

    @overload
    def policy_decisions(self, decision_type: type[DecisionT]) -> tuple[DecisionT, ...]: ...

    @overload
    def policy_decisions(self, decision_type: None = None) -> tuple[PolicyDecision, ...]: ...

    def policy_decisions(self, decision_type: type[Any] | None = None) -> tuple[Any, ...]:
        return self.scenario_run.policy_decisions(decision_type, rollout=self.rollout_index)

    @overload
    def market_observations(self, observation_type: type[ObservationT]) -> tuple[ObservationT, ...]: ...

    @overload
    def market_observations(self, observation_type: None = None) -> tuple[MarketObservation, ...]: ...

    def market_observations(self, observation_type: type[Any] | None = None) -> tuple[Any, ...]:
        return self.scenario_run.market_observations(observation_type, rollout=self.rollout_index)

    def journal_entries(self, *, journal_entry_type: JournalEntryType | None = None) -> tuple[JournalEntry, ...]:
        return self.scenario_run.journal_entries(journal_entry_type=journal_entry_type, rollout=self.rollout_index)

    def postings(self, *, role: ChartAccountRole | None = None, side: PostingSide | None = None) -> tuple[Posting, ...]:
        return self.scenario_run.postings(role=role, side=side, rollout=self.rollout_index)

    def balance_snapshots(self, *, role: ChartAccountRole | None = None) -> tuple[BalanceSnapshot, ...]:
        return self.scenario_run.balance_snapshots(role=role, rollout=self.rollout_index)

    def lot_dispositions(self, *, asset_class: LotAssetClass | None = None) -> tuple[LotDisposition, ...]:
        return self.scenario_run.lot_dispositions(asset_class=asset_class, rollout=self.rollout_index)

    @overload
    def accounting_details(self, detail_type: type[AccountingDetailT]) -> tuple[AccountingDetailT, ...]: ...

    @overload
    def accounting_details(self, detail_type: None = None) -> tuple[AccountingDetail, ...]: ...

    def accounting_details(self, detail_type: type[Any] | None = None) -> tuple[Any, ...]:
        return self.scenario_run.accounting_details(detail_type, rollout=self.rollout_index)

    def status(self) -> RolloutStatus:
        return self.scenario_run.rollout_status(self.rollout_index)


@dataclass(frozen=True)
class ScenarioRun:
    scenario: Scenario
    arrays: ScenarioRunArrays | None
    warnings: tuple[str, ...] = ()

    @property
    def scenario_id(self) -> str:
        return self.scenario.scenario_id

    def matrix(self, metric: ReportMetric) -> np.ndarray:
        value = self._metric_array(metric)
        if value.ndim != 2:
            raise KeyError(f"metric {metric!r} is not rollout/month shaped")
        return value.copy()

    def series(self, metric: ReportMetric, *, rollout: int = 0) -> np.ndarray:
        value = self._metric_array(metric)
        if value.ndim == 1:
            return value.copy()
        self._validate_rollout_index(rollout)
        return value[rollout, :].copy()

    def terminal(self, metric: ReportMetric, *, rollout: int | None = 0) -> float | np.ndarray:
        value = self._metric_array(metric)
        if value.ndim == 1:
            return float(value[-1])
        if rollout is None:
            return value[:, -1].copy()
        self._validate_rollout_index(rollout)
        return float(value[rollout, -1])

    def rollout(self, rollout_index: int) -> RolloutDetail:
        self._validate_rollout_index(rollout_index)
        return RolloutDetail(scenario_run=self, rollout_index=rollout_index)

    @overload
    def effects(self, effect_type: type[EffectT], *, rollout: int | None = None) -> tuple[EffectT, ...]: ...

    @overload
    def effects(self, effect_type: None = None, *, rollout: int | None = None) -> tuple[Effect, ...]: ...

    def effects(self, effect_type: type[Any] | None = None, *, rollout: int | None = None) -> tuple[Any, ...]:
        if self.arrays is None:
            return ()
        effects: tuple[Any, ...] = self.arrays.effects
        if effect_type is not None:
            effects = tuple(effect for effect in effects if isinstance(effect, effect_type))
        if rollout is not None:
            self._validate_rollout_index(rollout)
            effects = tuple(effect for effect in effects if effect.rollout_index == rollout)
        return effects

    @overload
    def policy_decisions(
        self, decision_type: type[DecisionT], *, rollout: int | None = None
    ) -> tuple[DecisionT, ...]: ...

    @overload
    def policy_decisions(
        self, decision_type: None = None, *, rollout: int | None = None
    ) -> tuple[PolicyDecision, ...]: ...

    def policy_decisions(
        self, decision_type: type[Any] | None = None, *, rollout: int | None = None
    ) -> tuple[Any, ...]:
        if self.arrays is None:
            return ()
        decisions: tuple[Any, ...] = self.arrays.policy_decisions
        if decision_type is not None:
            decisions = tuple(decision for decision in decisions if isinstance(decision, decision_type))
        if rollout is not None:
            self._validate_rollout_index(rollout)
            decisions = tuple(decision for decision in decisions if decision.rollout_index == rollout)
        return decisions

    @overload
    def market_observations(
        self, observation_type: type[ObservationT], *, rollout: int | None = None
    ) -> tuple[ObservationT, ...]: ...

    @overload
    def market_observations(
        self, observation_type: None = None, *, rollout: int | None = None
    ) -> tuple[MarketObservation, ...]: ...

    def market_observations(
        self, observation_type: type[Any] | None = None, *, rollout: int | None = None
    ) -> tuple[Any, ...]:
        if self.arrays is None:
            return ()
        observations: tuple[Any, ...] = self.arrays.market_observations
        if observation_type is not None:
            observations = tuple(
                observation for observation in observations if isinstance(observation, observation_type)
            )
        if rollout is not None:
            self._validate_rollout_index(rollout)
            observations = tuple(observation for observation in observations if observation.rollout_index == rollout)
        return observations

    def chart_accounts(self, *, role: ChartAccountRole | None = None) -> tuple[ChartAccount, ...]:
        if self.arrays is None:
            return ()
        accounts = self.arrays.chart_accounts
        if role is not None:
            accounts = tuple(account for account in accounts if account.role is role)
        return accounts

    def journal_entries(
        self, *, journal_entry_type: JournalEntryType | None = None, rollout: int | None = None
    ) -> tuple[JournalEntry, ...]:
        if self.arrays is None:
            return ()
        entries = self.arrays.journal_entries
        if journal_entry_type is not None:
            entries = tuple(entry for entry in entries if entry.journal_entry_type is journal_entry_type)
        if rollout is not None:
            self._validate_rollout_index(rollout)
            entries = tuple(entry for entry in entries if entry.rollout_index == rollout)
        return entries

    def postings(
        self, *, role: ChartAccountRole | None = None, side: PostingSide | None = None, rollout: int | None = None
    ) -> tuple[Posting, ...]:
        if self.arrays is None:
            return ()
        postings = self.arrays.postings
        if role is not None:
            account_by_id = {account.chart_account_id: account for account in self.arrays.chart_accounts}
            postings = tuple(posting for posting in postings if account_by_id[posting.chart_account_id].role is role)
        if side is not None:
            postings = tuple(posting for posting in postings if posting.side is side)
        if rollout is not None:
            self._validate_rollout_index(rollout)
            postings = tuple(posting for posting in postings if posting.rollout_index == rollout)
        return postings

    def balance_snapshots(
        self, *, role: ChartAccountRole | None = None, rollout: int | None = None
    ) -> tuple[BalanceSnapshot, ...]:
        if self.arrays is None:
            return ()
        snapshots = self.arrays.balance_snapshots
        if role is not None:
            account_by_id = {account.chart_account_id: account for account in self.arrays.chart_accounts}
            snapshots = tuple(
                snapshot for snapshot in snapshots if account_by_id[snapshot.chart_account_id].role is role
            )
        if rollout is not None:
            self._validate_rollout_index(rollout)
            snapshots = tuple(snapshot for snapshot in snapshots if snapshot.rollout_index == rollout)
        return snapshots

    def tax_lots(self, *, asset_class: LotAssetClass | None = None) -> tuple[TaxLot, ...]:
        if self.arrays is None:
            return ()
        lots = self.arrays.tax_lots
        if asset_class is not None:
            lots = tuple(lot for lot in lots if lot.asset_class is asset_class)
        return lots

    def lot_dispositions(
        self, *, asset_class: LotAssetClass | None = None, rollout: int | None = None
    ) -> tuple[LotDisposition, ...]:
        if self.arrays is None:
            return ()
        dispositions = self.arrays.lot_dispositions
        if asset_class is not None:
            dispositions = tuple(disposition for disposition in dispositions if disposition.asset_class is asset_class)
        if rollout is not None:
            self._validate_rollout_index(rollout)
            dispositions = tuple(disposition for disposition in dispositions if disposition.rollout_index == rollout)
        return dispositions

    def liabilities(self) -> tuple[LiabilityState, ...]:
        if self.arrays is None:
            return ()
        return self.arrays.liabilities

    @overload
    def accounting_details(
        self, detail_type: type[AccountingDetailT], *, rollout: int | None = None
    ) -> tuple[AccountingDetailT, ...]: ...

    @overload
    def accounting_details(
        self, detail_type: None = None, *, rollout: int | None = None
    ) -> tuple[AccountingDetail, ...]: ...

    def accounting_details(
        self, detail_type: type[Any] | None = None, *, rollout: int | None = None
    ) -> tuple[Any, ...]:
        if self.arrays is None:
            return ()
        details: tuple[Any, ...] = self.arrays.accounting_details
        if detail_type is not None:
            details = tuple(detail for detail in details if isinstance(detail, detail_type))
        if rollout is not None:
            self._validate_rollout_index(rollout)
            details = tuple(detail for detail in details if detail.rollout_index == rollout)
        return details

    def rollout_statuses(self) -> tuple[RolloutStatus, ...]:
        if self.arrays is None:
            return ()
        return self.arrays.rollout_statuses()

    def rollout_status_summary(self) -> RolloutStatusSummary:
        return RolloutStatusSummary.from_statuses(self.rollout_statuses())

    def rollout_status(self, rollout: int) -> RolloutStatus:
        self._validate_rollout_index(rollout)
        return self.rollout_statuses()[rollout]

    def to_response_result(
        self, report_spec: ReportSpec | None = None, *, exogenous_paths: tuple[ExogenousPathIdentity, ...] = ()
    ) -> ScenarioResult:
        report_spec = report_spec or ReportSpec()
        if self.arrays is None:
            return ScenarioResult(
                scenario_id=self.scenario.scenario_id,
                scenario_label=self.scenario.label,
                summary=_accepted_summary(self.scenario),
                warnings=self.warnings,
            )
        rollout_statuses = self.rollout_statuses()
        return ScenarioResult(
            scenario_id=self.scenario.scenario_id,
            scenario_label=self.scenario.label,
            summary=_accepted_summary(self.scenario),
            projection_trajectories=_projection_trajectory_identities(
                scenario_id=self.scenario.scenario_id,
                scenario_input_id=scenario_input_id(self.scenario),
                policies=self.scenario.policies,
                exogenous_paths=exogenous_paths,
            ),
            rollout_statuses=rollout_statuses,
            metric_fan_columns=self.arrays.metric_fan_columns(),
            monthly_columns=self.arrays.monthly_columns() if report_spec.include_monthly_columns else None,
            terminal_columns=self.arrays.terminal_columns(),
            effects=self.arrays.effects,
            policy_decisions=self.arrays.policy_decisions,
            market_observations=self.arrays.market_observations,
            chart_accounts=self.arrays.chart_accounts,
            journal_entries=self.arrays.journal_entries,
            postings=self.arrays.postings,
            balance_snapshots=self.arrays.balance_snapshots,
            tax_lots=self.arrays.tax_lots,
            lot_dispositions=self.arrays.lot_dispositions,
            liabilities=self.arrays.liabilities,
            accounting_details=self.arrays.accounting_details,
            obligations=self.arrays.obligations,
            funding_decisions=self.arrays.funding_decisions,
            settlement_results=self.arrays.settlement_results,
            failure_events=self.arrays.failure_events,
            warnings=self.warnings,
        )

    def _metric_array(self, metric: ReportMetric) -> np.ndarray:
        if self.arrays is None:
            raise ValueError(f"scenario {self.scenario_id!r} was not simulated")
        return report_metric_array(self.arrays, metric)

    def _validate_rollout_index(self, rollout: int) -> None:
        if self.arrays is None:
            raise ValueError(f"scenario {self.scenario_id!r} was not simulated")
        if rollout < 0 or rollout >= self.arrays.rollout_count:
            raise IndexError(
                f"rollout index {rollout} out of range for scenario {self.scenario_id!r} "
                f"with {self.arrays.rollout_count} rollouts"
            )


@dataclass(frozen=True)
class ScenarioSetRun:
    scenario_set: ScenarioSet
    market_bundle: MarketBundle
    scenario_runs: tuple[ScenarioRun, ...]
    warnings: tuple[str, ...] = ()

    def scenario(self, scenario_id: str) -> ScenarioRun:
        for scenario_run in self.scenario_runs:
            if scenario_run.scenario_id == scenario_id:
                return scenario_run
        available = ", ".join(run.scenario_id for run in self.scenario_runs)
        raise KeyError(f"unknown scenario {scenario_id!r}; available scenarios: {available}")

    def to_response(self) -> ScenarioSetRunResponse:
        exogenous_paths = _exogenous_path_identities(self.market_bundle.metadata)
        scenario_input_ids = tuple(scenario_input_id(scenario) for scenario in self.scenario_set.scenarios)
        return ScenarioSetRunResponse(
            scenario_set_id=self.scenario_set.scenario_set_id,
            request=self.scenario_set,
            market_request=self.scenario_set.market_request,
            report_spec=self.scenario_set.report_spec,
            market_metadata=self.market_bundle.metadata.to_json_dict(),
            projection_run=ProjectionRun(
                projection_run_id=projection_run_id(
                    scenario_set_id=self.scenario_set.scenario_set_id,
                    path_set_id=self.market_bundle.metadata.path_set_id,
                    scenario_input_ids=scenario_input_ids,
                ),
                scenario_set_id=self.scenario_set.scenario_set_id,
                path_set_id=self.market_bundle.metadata.path_set_id,
                scenario_input_ids=scenario_input_ids,
            ),
            exogenous_paths=exogenous_paths,
            scenario_results=tuple(
                scenario_run.to_response_result(self.scenario_set.report_spec, exogenous_paths=exogenous_paths)
                for scenario_run in self.scenario_runs
            ),
            warnings=self.warnings,
        )


def simulate_set(
    scenario_set: ScenarioSet,
    *,
    market_provider: MarketBundleProvider | None = None,
    market_bundle: MarketBundle | None = None,
) -> ScenarioSetRun:
    """Simulate a typed scenario set and return a distribution-first result object."""

    if market_provider is not None and market_bundle is not None:
        raise ValueError("pass either market_provider or market_bundle, not both")
    validate_scenario_set(scenario_set)
    if market_bundle is None:
        provider = market_provider or SimpleMarketBundleProvider()
        market_bundle = sample_market_bundle_for_request(
            provider, scenario_set.market_request, required_keys=_extract_required_market_keys(scenario_set)
        )
    _validate_market_bundle_matches_request(scenario_set, market_bundle)

    scenario_runs: list[ScenarioRun] = []
    for scenario in scenario_set.scenarios:
        if not scenario.enabled:
            scenario_runs.append(ScenarioRun(scenario=scenario, arrays=None))
            continue
        scenario_runs.append(ScenarioRun(scenario=scenario, arrays=run_scenario_vectorized(scenario, market_bundle)))
    return ScenarioSetRun(scenario_set=scenario_set, market_bundle=market_bundle, scenario_runs=tuple(scenario_runs))


def _extract_required_market_keys(scenario_set: ScenarioSet) -> RequiredMarketKeys:
    """Collect the per-asset / per-location keys every scenario in the set will look up.

    Providers must populate exactly these on the resulting `MarketBundle`; the
    bundle's lookup helpers raise `MissingMarketFactorError` for any key not in
    the dict, so this set is the contract the provider has to honor.
    """
    location_ids: set[str] = set()
    pe_issuer_ids: set[str] = set()
    crypto_symbols: set[str] = set()
    for scenario in scenario_set.scenarios:
        if scenario.location_id is not None:
            location_ids.add(scenario.location_id)
        for position in scenario.initial_balance_sheet.assets:
            if isinstance(position, PrivateEquityPosition):
                pe_issuer_ids.add(position.market_routing_key)
            elif isinstance(position, CryptoAssetPosition):
                crypto_symbols.add(position.asset_symbol)
    return RequiredMarketKeys(
        location_ids=frozenset(location_ids),
        pe_issuer_ids=frozenset(pe_issuer_ids),
        crypto_symbols=frozenset(crypto_symbols),
    )


def _exogenous_path_identities(metadata: Any) -> tuple[ExogenousPathIdentity, ...]:
    return tuple(
        ExogenousPathIdentity(
            rollout_index=rollout_index,
            path_set_id=metadata.path_set_id,
            exogenous_path_id=exogenous_path_id,
            market_model_id=metadata.market_model_id,
            market_model_version_id=metadata.market_model_version_id,
            scenario_generator_id=metadata.scenario_generator_id,
            scenario_generator_version_id=metadata.scenario_generator_version_id,
            evidence_set_id=metadata.evidence_set_id,
            calibration_artifact_id=metadata.calibration_artifact_id,
            risk_factor_set_id=metadata.risk_factor_set_id,
            seed=metadata.seed,
            event_stream_ids=metadata.event_stream_ids,
        )
        for rollout_index, exogenous_path_id in enumerate(metadata.exogenous_path_ids)
    )


def _projection_trajectory_identities(
    *,
    scenario_id: str,
    scenario_input_id: str,
    policies: tuple[Any, ...] = (),
    exogenous_paths: tuple[ExogenousPathIdentity, ...],
) -> tuple[ProjectionTrajectoryIdentity, ...]:
    scenario_policy_program_set_id = policy_program_set_id(scenario_id=scenario_id, policies=policies)
    return tuple(
        ProjectionTrajectoryIdentity(
            scenario_id=scenario_id,
            rollout_index=path.rollout_index,
            path_set_id=path.path_set_id,
            exogenous_path_id=path.exogenous_path_id,
            scenario_input_id=scenario_input_id,
            policy_program_set_id=scenario_policy_program_set_id,
            projection_trajectory_id=projection_trajectory_id(
                scenario_id=scenario_id,
                scenario_input_id=scenario_input_id,
                exogenous_path_id=path.exogenous_path_id,
                policy_program_set_id=scenario_policy_program_set_id,
            ),
        )
        for path in exogenous_paths
    )


def validate_scenario_set(scenario_set: ScenarioSet) -> None:
    errors: list[str] = []
    for scenario_index, scenario in enumerate(scenario_set.scenarios):
        errors.extend(_validate_scenario(scenario, f"scenarios[{scenario_index}]"))
    if errors:
        raise ScenarioValidationError("; ".join(errors))


def _validate_scenario(scenario: Scenario, path: str) -> list[str]:
    errors: list[str] = []
    actor_ids = [actor.actor_id for actor in scenario.actors]
    actor_id_set = set(actor_ids)
    _append_duplicate_errors(errors, actor_ids, f"{path}.actors", "actor_id")

    primary_owners = [actor.actor_id for actor in scenario.actors if actor.role is ActorRole.PRIMARY_OWNER]
    if len(primary_owners) > 1:
        errors.append(f"{path}.actors must contain at most one primary_owner actor, got {primary_owners}")

    balance_sheet = scenario.initial_balance_sheet
    _append_duplicate_errors(
        errors,
        [account.account_id for account in balance_sheet.accounts],
        f"{path}.initial_balance_sheet.accounts",
        "account_id",
    )
    _append_duplicate_errors(
        errors, [asset.asset_id for asset in balance_sheet.assets], f"{path}.initial_balance_sheet.assets", "asset_id"
    )
    _append_duplicate_errors(errors, [event.event_id for event in scenario.events], f"{path}.events", "event_id")
    _append_duplicate_errors(
        errors, [policy.policy_id for policy in scenario.policies], f"{path}.policies", "policy_id"
    )

    for index, account in enumerate(balance_sheet.accounts):
        _validate_actor_ref(
            errors,
            actor_id_set,
            account.owner_actor_id,
            f"{path}.initial_balance_sheet.accounts[{index}].owner_actor_id",
        )
    for index, asset in enumerate(balance_sheet.assets):
        _validate_actor_ref(
            errors, actor_id_set, asset.owner_actor_id, f"{path}.initial_balance_sheet.assets[{index}].owner_actor_id"
        )
    for index, policy in enumerate(scenario.policies):
        _validate_actor_ref(errors, actor_id_set, policy.actor_id, f"{path}.policies[{index}].actor_id")
        if isinstance(policy, PartnerEquityAccrualPolicy):
            _validate_partner_equity_policy(errors, scenario, policy, f"{path}.policies[{index}]")
    for index, event in enumerate(scenario.events):
        if event.actor_id is not None:
            _validate_actor_ref(errors, actor_id_set, event.actor_id, f"{path}.events[{index}].actor_id")

    known_property_ids = _known_property_ids(scenario)
    owner_residence_property_id = scenario.occupancy_plan.owner_residence_property_id
    if owner_residence_property_id is not None and owner_residence_property_id not in known_property_ids:
        errors.append(
            f"{path}.occupancy_plan.owner_residence_property_id references unknown property "
            f"{owner_residence_property_id!r}"
        )
    _validate_property_selection(errors, scenario, path)
    _validate_rental_plan(errors, scenario, path)
    for index, event in enumerate(scenario.events):
        if event.property_id is None:
            continue
        if event.event_type is EventType.PROPERTY_PURCHASE:
            continue
        if event.property_id not in known_property_ids:
            errors.append(f"{path}.events[{index}].property_id references unknown property {event.property_id!r}")
    return errors


def _validate_property_selection(errors: list[str], scenario: Scenario, path: str) -> None:
    selection = scenario.property_selection
    if selection.property_id is None:
        return
    if selection.location_id is None:
        errors.append(f"{path}.property_selection.location_id is required when property_id is set")
    if selection.purchase_price_usd is None:
        errors.append(f"{path}.property_selection.purchase_price_usd is required when property_id is set")


def _validate_rental_plan(errors: list[str], scenario: Scenario, path: str) -> None:
    rental = scenario.rental_plan
    if rental.rental_mode is not RentalMode.NOT_RENTED and scenario.property_selection.property_id is None:
        errors.append(f"{path}.rental_plan.rental_mode requires property_selection.property_id")


def _validate_partner_equity_policy(
    errors: list[str], scenario: Scenario, policy: PartnerEquityAccrualPolicy, path: str
) -> None:
    selected_property_id = scenario.property_selection.property_id
    if selected_property_id is None:
        errors.append(f"{path}.property_id requires property_selection.property_id")
        return
    if policy.property_id is not None and policy.property_id != selected_property_id:
        errors.append(
            f"{path}.property_id references {policy.property_id!r}, but scenario selects {selected_property_id!r}"
        )


def _known_property_ids(scenario: Scenario) -> set[str]:
    property_ids: set[str] = set()
    if scenario.property_selection.property_id is not None:
        property_ids.add(scenario.property_selection.property_id)
    return property_ids


def _validate_actor_ref(errors: list[str], actor_ids: set[str], actor_id: str, path: str) -> None:
    if actor_id not in actor_ids:
        errors.append(f"{path} references unknown actor {actor_id!r}")


def _append_duplicate_errors(errors: list[str], values: list[str], path: str, field_name: str) -> None:
    duplicate_values = sorted({value for value in values if values.count(value) > 1})
    if duplicate_values:
        errors.append(f"{path} contains duplicate {field_name} values: {duplicate_values}")


def _validate_market_bundle_matches_request(scenario_set: ScenarioSet, market_bundle: MarketBundle) -> None:
    request = scenario_set.market_request
    errors: list[str] = []
    if market_bundle.rollout_count != int(request.rollout_count):
        errors.append(
            "market_bundle.rollout_count "
            f"{market_bundle.rollout_count} does not match market_request.rollout_count {request.rollout_count}"
        )
    if market_bundle.horizon_months != int(request.horizon_months):
        errors.append(
            "market_bundle.horizon_months "
            f"{market_bundle.horizon_months} does not match market_request.horizon_months {request.horizon_months}"
        )
    if errors:
        raise ScenarioValidationError("; ".join(errors))


def _accepted_summary(scenario: Scenario) -> ScenarioAcceptedSummary:
    return ScenarioAcceptedSummary(
        enabled=scenario.enabled, property_id=scenario.property_selection.property_id, location_id=scenario.location_id
    )
