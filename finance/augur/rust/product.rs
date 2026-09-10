//! The seven base product metric series, reduced per snapshot for one selected agent.
//!
//! `finance/augur/sim/metric_composition.py` composes `home_equity`,
//! `liquid_net_worth` and `net_worth` from these, and does so once for every backend —
//! so this module deliberately stops at the base series and derives nothing.
//!
//! The reduction runs for every capture mode, including `Summary`: the percentile-fan
//! workload wants these series without paying for dense monthly snapshots.

use std::collections::BTreeMap;

use crate::execution::{
    BondState, ExecutionInput, MortgageSnapshot, PropertyState, TlhPortfolioObservation,
};
use crate::holdings::{AgentHoldings, HoldingsError, LotView};
use crate::ledger::{Ledger, LedgerError};
use crate::money::{Money, PerUnit};
use crate::property::Valuation;

/// Base metrics per snapshot, in `metric_composition.BASE_METRIC_NAMES` order.
pub const BASE_METRIC_NAMES: [&str; 7] = [
    "cash_quanta",
    "holding_value_quanta",
    "private_equity_value_quanta",
    "property_value_quanta",
    "mortgage_balance_quanta",
    "shortfall_quanta",
    "bond_value_quanta",
];

pub const BASE_METRIC_COUNT: usize = BASE_METRIC_NAMES.len();

/// One snapshot's base metrics, indexed by `BASE_METRIC_NAMES` position.
pub type BaseMetrics = [i64; BASE_METRIC_COUNT];

const CASH: usize = 0;
const HOLDING: usize = 1;
const PRIVATE_EQUITY: usize = 2;
const PROPERTY: usize = 3;
const MORTGAGE: usize = 4;
const SHORTFALL: usize = 5;
const BOND: usize = 6;

/// Everything the reduction needs that does not change month to month, resolved once per
/// fixture. Series are resolved to row indices here because the reduction runs on every
/// snapshot of every rollout, where `engine::series_value`'s name scan would dominate.
#[derive(Clone, Debug)]
pub struct ProductInputs {
    holdings: AgentHoldings,
    /// `private_equity_mark:<issuer>` row for each issuer the selected agent can hold.
    private_equity_mark_by_issuer: BTreeMap<String, usize>,
    /// Keyed by `property_id`; a property whose home-value series is absent is omitted and
    /// contributes nothing rather than reducing over a series that is not there.
    property_valuations: BTreeMap<String, Valuation>,
}

impl ProductInputs {
    pub fn primary_agent_id(&self) -> &str {
        self.holdings.agent_id()
    }

    pub fn resolve(fixture: &ExecutionInput, primary_agent_id: &str) -> Result<Self, ProductError> {
        let holdings = AgentHoldings::resolve(fixture, primary_agent_id)?;
        let series_rows: BTreeMap<&str, usize> = fixture
            .series
            .iter()
            .enumerate()
            .map(|(row, series)| (series.series_id.as_str(), row))
            .collect();

        let mut private_equity_mark_by_issuer = BTreeMap::new();
        let mut register_asset = |asset_id: &str| -> Result<(), ProductError> {
            if let Some(issuer_id) = asset_id
                .strip_prefix("private_equity:")
                .filter(|id| !id.is_empty())
            {
                let series_id = format!("private_equity_mark:{issuer_id}");
                let row = series_rows
                    .get(series_id.as_str())
                    .ok_or(ProductError::MissingSeries { series_id })?;
                private_equity_mark_by_issuer.insert(issuer_id.to_owned(), *row);
            }
            Ok(())
        };
        for pool in &fixture.scenario.holding_pools {
            if pool.agent_id == primary_agent_id {
                register_asset(&pool.asset_id)?;
            }
        }

        let property_valuations = fixture
            .scenario
            .scheduled_property_purchases
            .iter()
            .filter(|purchase| purchase.buyer_agent_id == primary_agent_id)
            .filter_map(|purchase| {
                let series_id = format!("home_value:{}", purchase.location_id);
                let row = series_rows.get(series_id.as_str())?;
                Some((purchase.property_id.clone(), Valuation::new(purchase, *row)))
            })
            .collect();

        Ok(Self {
            holdings,
            private_equity_mark_by_issuer,
            property_valuations,
        })
    }
}

#[derive(Debug, thiserror::Error, Eq, PartialEq)]
pub enum ProductError {
    #[error(transparent)]
    Property(#[from] crate::property::ValuationError),
    #[error(transparent)]
    Holdings(#[from] HoldingsError),
    #[error(transparent)]
    Ledger(#[from] LedgerError),
    #[error("product metrics need series {series_id:?}, which the fixture does not supply")]
    MissingSeries { series_id: String },
    #[error("series {series_id:?} has no value at rollout {rollout} snapshot {snapshot}")]
    MissingSeriesValue {
        series_id: String,
        rollout: u32,
        snapshot: u32,
    },
    #[error("rollout produced {actual} product snapshots, expected {expected}")]
    SnapshotCount { expected: usize, actual: usize },
    #[error(transparent)]
    Arithmetic(#[from] crate::money::ArithmeticError),
}

fn series_at(
    fixture: &ExecutionInput,
    row: usize,
    rollout: u32,
    snapshot: u32,
) -> Result<i64, ProductError> {
    let series = &fixture.series[row];
    series
        .value(rollout, snapshot)
        .ok_or_else(|| ProductError::MissingSeriesValue {
            series_id: series.series_id.clone(),
            rollout,
            snapshot,
        })
}

/// What the reduction reads out of the live engine state at one snapshot.
pub struct SnapshotState<'a> {
    pub ledger: &'a Ledger,
    pub lots: &'a [LotView<'a>],
    pub tlh_portfolios: &'a [TlhPortfolioObservation],
    pub properties: &'a [PropertyState],
    pub mortgages: &'a [MortgageSnapshot],
    /// This snapshot's bond states, already CPI-indexed and zeroed for matured bonds
    /// by `engine::bond_states`.
    pub bonds: &'a [BondState],
    /// The month's settled-obligation shortfall for the selected agent. Zero at snapshot 0
    /// There are no observations after stopping.
    pub shortfall: Money,
}

/// Reduce one snapshot to its base metrics.
///
/// State and marks must refer to the same observed point, including a book at stopping.
pub fn snapshot_metrics(
    fixture: &ExecutionInput,
    inputs: &ProductInputs,
    state: &SnapshotState<'_>,
    rollout: u32,
    snapshot: u32,
) -> Result<BaseMetrics, ProductError> {
    let mut metrics: BaseMetrics = [0; BASE_METRIC_COUNT];

    metrics[CASH] = inputs.holdings.cash(state.ledger)?.0;
    metrics[HOLDING] = inputs
        .holdings
        .public_value(fixture, state.lots.iter().copied(), rollout, snapshot)?
        .0;

    for portfolio in state
        .tlh_portfolios
        .iter()
        .filter(|portfolio| portfolio.owner_agent_id == inputs.primary_agent_id())
    {
        metrics[HOLDING] = Money(metrics[HOLDING]).checked_add(portfolio.value)?.0;
    }

    for lot in state.lots {
        if lot.agent_id != inputs.primary_agent_id() || lot.units_remaining == 0 {
            continue;
        }
        let Some(issuer_id) = lot
            .asset_id
            .strip_prefix("private_equity:")
            .filter(|id| !id.is_empty())
        else {
            continue;
        };
        let Some(&row) = inputs.private_equity_mark_by_issuer.get(issuer_id) else {
            continue;
        };
        let value = lot
            .value(PerUnit(series_at(fixture, row, rollout, snapshot)?))?
            .0;
        metrics[PRIVATE_EQUITY] = metrics[PRIVATE_EQUITY].checked_add(value).ok_or(
            crate::money::ArithmeticError::Overflow {
                operation: "product holding total",
            },
        )?;
    }

    for property in state.properties.iter().filter(|property| property.active) {
        let Some(valuation) = inputs.property_valuations.get(&property.property_id) else {
            continue;
        };
        let market = valuation.at(fixture, rollout, snapshot)?.0;
        metrics[PROPERTY] = metrics[PROPERTY].checked_add(market).ok_or(
            crate::money::ArithmeticError::Overflow {
                operation: "product property total",
            },
        )?;
    }

    for mortgage in state.mortgages {
        if mortgage.agent_id != inputs.primary_agent_id() {
            continue;
        }
        metrics[MORTGAGE] = metrics[MORTGAGE].checked_add(mortgage.principal.0).ok_or(
            crate::money::ArithmeticError::Overflow {
                operation: "product mortgage total",
            },
        )?;
    }

    metrics[SHORTFALL] = state.shortfall.0;

    for bond in state.bonds {
        if bond.agent_id != inputs.primary_agent_id() {
            continue;
        }
        metrics[BOND] = metrics[BOND].checked_add(bond.principal.0).ok_or(
            crate::money::ArithmeticError::Overflow {
                operation: "product bond total",
            },
        )?;
    }

    Ok(metrics)
}

/// Every base metric series for a whole population, in the layout the Python product
/// read model consumes.
#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize)]
pub struct ProductMetricSeries {
    pub rollout_count: u32,
    pub snapshot_count: u32,
    /// One entry per `BASE_METRIC_NAMES` position, each a row-major `[snapshot][rollout]`
    /// block. Values after `failed_month + 1` are unobserved transport padding, not money.
    pub base_series: Vec<Vec<i64>>,
    /// Per-rollout failure month, `-1` for a rollout that never failed.
    pub failed_month: Vec<i64>,
}

impl ProductMetricSeries {
    /// Assemble the population series from per-rollout snapshot rows, transposing the
    /// engine's rollout-major execution into the snapshot-major product layout.
    pub fn from_rollouts(
        snapshot_count: u32,
        rollouts: &[(Vec<BaseMetrics>, Option<u32>)],
    ) -> Result<Self, ProductError> {
        let rollout_count = rollouts.len();
        let snapshots = snapshot_count as usize;
        for (metrics, failed) in rollouts {
            let expected = failed.map_or(snapshots, |month| month as usize + 2);
            if metrics.len() != expected {
                return Err(ProductError::SnapshotCount {
                    expected,
                    actual: metrics.len(),
                });
            }
        }
        let base_series = (0..BASE_METRIC_COUNT)
            .map(|metric| {
                let mut block = vec![0_i64; snapshots * rollout_count];
                for (rollout, (metrics, _)) in rollouts.iter().enumerate() {
                    for (snapshot, row) in metrics.iter().enumerate() {
                        block[snapshot * rollout_count + rollout] = row[metric];
                    }
                }
                block
            })
            .collect();
        Ok(Self {
            rollout_count: rollout_count as u32,
            snapshot_count,
            base_series,
            failed_month: rollouts
                .iter()
                .map(|(_, failed)| failed.map_or(-1, i64::from))
                .collect(),
        })
    }
}
