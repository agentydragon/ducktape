//! The seven base product metric series, reduced per snapshot for one selected agent.
//!
//! `finance/augur/product/metric_composition.py` composes `home_equity`,
//! `liquid_net_worth` and `net_worth` from these, and does so once for every backend —
//! so this module deliberately stops at the base series and derives nothing.
//!
//! The reduction runs for every capture mode, including `Summary`: the percentile-fan
//! workload wants these series without paying for dense monthly snapshots, which is the
//! same split JAX draws with `emit_dense=False`.

use std::collections::BTreeMap;

use crate::fixture::{BondState, Fixture, MortgageState, PropertyState};
use crate::ledger::{AccountRef, Ledger};
use crate::money::{Money, mul_div_round_half_up};

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

/// A property's static valuation terms: what the agent paid, and the home-value path
/// the price is escalated along.
#[derive(Clone, Debug)]
struct PropertyValuation {
    purchase_price: Money,
    purchase_month: u32,
    home_value_series: usize,
}

/// Everything the reduction needs that does not change month to month, resolved once per
/// fixture. Series are resolved to row indices here because the reduction runs on every
/// snapshot of every rollout, where `engine::series_value`'s name scan would dominate.
#[derive(Clone, Debug)]
pub struct ProductInputs {
    cash_accounts: Vec<AccountRef>,
    /// `security:<asset_id>` row for each public asset the selected agent can hold.
    public_series_by_asset: BTreeMap<String, usize>,
    /// `private_equity_mark:<issuer>` row for each issuer the selected agent can hold.
    private_equity_mark_by_issuer: BTreeMap<String, usize>,
    /// Keyed by `property_id`; a property whose home-value series is absent is omitted and
    /// contributes nothing, matching the JAX reducer's `valid_series` gate.
    property_valuations: BTreeMap<String, PropertyValuation>,
    primary_agent_id: String,
}

impl ProductInputs {
    pub fn primary_agent_id(&self) -> &str {
        &self.primary_agent_id
    }

    pub fn resolve(fixture: &Fixture, primary_agent_id: &str) -> Result<Self, ProductError> {
        if !fixture
            .scenario
            .accounts
            .iter()
            .any(|spec| spec.account.agent_id == primary_agent_id)
        {
            return Err(ProductError::UnknownPrimaryAgent {
                agent_id: primary_agent_id.into(),
            });
        }
        let series_rows: BTreeMap<&str, usize> = fixture
            .series
            .iter()
            .enumerate()
            .map(|(row, series)| (series.series_id.as_str(), row))
            .collect();

        let cash_accounts = fixture
            .scenario
            .accounts
            .iter()
            .filter(|spec| spec.account.agent_id == primary_agent_id)
            .map(|spec| spec.account.clone())
            .collect();

        let mut public_series_by_asset = BTreeMap::new();
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
                return Ok(());
            }
            let series_id = format!("security:{asset_id}");
            let row = series_rows
                .get(series_id.as_str())
                .ok_or(ProductError::MissingSeries { series_id })?;
            public_series_by_asset.insert(asset_id.to_owned(), *row);
            Ok(())
        };
        for lot in &fixture.scenario.initial_lots {
            if lot.agent_id == primary_agent_id {
                register_asset(&lot.asset_id)?;
            }
        }
        for policy in &fixture.scenario.target_allocation_policies {
            if policy.agent_id == primary_agent_id {
                for sleeve in &policy.sleeves {
                    register_asset(&sleeve.asset_id)?;
                }
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
                Some((
                    purchase.property_id.clone(),
                    PropertyValuation {
                        purchase_price: purchase.purchase_price,
                        purchase_month: purchase.month,
                        home_value_series: *row,
                    },
                ))
            })
            .collect();

        Ok(Self {
            cash_accounts,
            public_series_by_asset,
            private_equity_mark_by_issuer,
            property_valuations,
            primary_agent_id: primary_agent_id.to_owned(),
        })
    }
}

#[derive(Debug, thiserror::Error, Eq, PartialEq)]
pub enum ProductError {
    #[error("scenario has no account for primary agent {agent_id:?}")]
    UnknownPrimaryAgent { agent_id: String },
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
    fixture: &Fixture,
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

/// One live lot, borrowed from the engine's own state for the duration of the reduction.
pub struct LotView<'a> {
    pub agent_id: &'a str,
    pub asset_id: &'a str,
    pub units_remaining: i64,
    pub quantity_scale: i64,
}

/// What the reduction reads out of the live engine state at one snapshot.
pub struct SnapshotState<'a> {
    pub ledger: &'a Ledger,
    pub lots: &'a [LotView<'a>],
    pub properties: &'a [PropertyState],
    pub mortgages: &'a [MortgageState],
    /// This snapshot's bond states, already CPI-indexed and zeroed for matured bonds
    /// by `engine::bond_states`.
    pub bonds: &'a [BondState],
    /// The month's settled-obligation shortfall for the selected agent. Zero at snapshot 0
    /// and for every snapshot after the rollout freezes.
    pub shortfall: Money,
    pub failed: bool,
}

/// Reduce one snapshot to its base metrics.
///
/// Failure semantics follow the JAX reducer exactly, including where it is uneven: dollar
/// state (cash, lots, mortgage principal) is already drained by the caller, and bonds are
/// zeroed here because their face is a static input the freeze never touches. Property
/// value is deliberately NOT zeroed — see `docs/product_metrics.md` § Failed rollouts.
pub fn snapshot_metrics(
    fixture: &Fixture,
    inputs: &ProductInputs,
    state: &SnapshotState<'_>,
    rollout: u32,
    snapshot: u32,
) -> Result<BaseMetrics, ProductError> {
    let mut metrics: BaseMetrics = [0; BASE_METRIC_COUNT];

    for account in &inputs.cash_accounts {
        let balance = state
            .ledger
            .balance(account)
            .map(|money| money.0)
            .unwrap_or(0);
        metrics[CASH] =
            metrics[CASH]
                .checked_add(balance)
                .ok_or(crate::money::ArithmeticError::Overflow {
                    operation: "product cash total",
                })?;
    }

    for lot in state.lots {
        if lot.agent_id != inputs.primary_agent_id || lot.units_remaining == 0 {
            continue;
        }
        let (slot, series_row) = match lot
            .asset_id
            .strip_prefix("private_equity:")
            .filter(|id| !id.is_empty())
        {
            Some(issuer_id) => match inputs.private_equity_mark_by_issuer.get(issuer_id) {
                Some(row) => (PRIVATE_EQUITY, *row),
                None => continue,
            },
            None => match inputs.public_series_by_asset.get(lot.asset_id) {
                Some(row) => (HOLDING, *row),
                None => continue,
            },
        };
        let price = series_at(fixture, series_row, rollout, snapshot)?;
        let value = mul_div_round_half_up(
            lot.units_remaining,
            price,
            lot.quantity_scale,
            "product holding value",
        )?;
        metrics[slot] =
            metrics[slot]
                .checked_add(value)
                .ok_or(crate::money::ArithmeticError::Overflow {
                    operation: "product holding total",
                })?;
    }

    for property in state.properties.iter().filter(|property| property.active) {
        let Some(valuation) = inputs.property_valuations.get(&property.property_id) else {
            continue;
        };
        let base = series_at(
            fixture,
            valuation.home_value_series,
            rollout,
            valuation.purchase_month,
        )?;
        if base <= 0 {
            continue;
        }
        let current = series_at(fixture, valuation.home_value_series, rollout, snapshot)?;
        let market = mul_div_round_half_up(
            valuation.purchase_price.0,
            current,
            base,
            "product property value",
        )?;
        metrics[PROPERTY] = metrics[PROPERTY].checked_add(market).ok_or(
            crate::money::ArithmeticError::Overflow {
                operation: "product property total",
            },
        )?;
    }

    for mortgage in state.mortgages {
        if mortgage.agent_id != inputs.primary_agent_id {
            continue;
        }
        metrics[MORTGAGE] = metrics[MORTGAGE].checked_add(mortgage.principal.0).ok_or(
            crate::money::ArithmeticError::Overflow {
                operation: "product mortgage total",
            },
        )?;
    }

    metrics[SHORTFALL] = state.shortfall.0;

    if !state.failed {
        for bond in state.bonds {
            if bond.agent_id != inputs.primary_agent_id {
                continue;
            }
            metrics[BOND] = metrics[BOND].checked_add(bond.principal.0).ok_or(
                crate::money::ArithmeticError::Overflow {
                    operation: "product bond total",
                },
            )?;
        }
    }

    Ok(metrics)
}

/// Every base metric series for a whole population, in the layout the Python product
/// read model consumes.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProductMetricSeries {
    pub rollout_count: u32,
    pub snapshot_count: u32,
    /// One entry per `BASE_METRIC_NAMES` position, each a row-major `[snapshot][rollout]`
    /// block — the `(snapshot, rollout)` shape `ProductMetricArrays.base_series` expects.
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
        for (metrics, _) in rollouts {
            if metrics.len() != snapshots {
                return Err(ProductError::SnapshotCount {
                    expected: snapshots,
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
