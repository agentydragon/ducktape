//! Security lots and their prices: distributions, FIFO sales, and held-to-maturity
//! nominal bonds and TIPS.

use super::*;

#[derive(Clone, Debug)]
pub(super) struct LotState {
    pub(super) spec: InitialLotSpec,
    pub(super) units_remaining: Quantity,
    pub(super) basis_remaining: Money,
}

impl LotState {
    pub(super) fn view(&self) -> LotView<'_> {
        LotView {
            agent_id: &self.spec.agent_id,
            asset_id: &self.spec.asset_id,
            units_remaining: self.units_remaining.0,
            quantity_scale: self.spec.quantity_scale,
        }
    }
}

pub(super) fn canonical_lot_asset_id(asset_id: &str) -> String {
    if private_equity_issuer(asset_id).is_some() {
        asset_id.to_owned()
    } else {
        format!("security:{asset_id}")
    }
}

pub(super) fn execute_distributions(
    fixture: &ExecutionInput,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &[LotState],
    tax: &mut TaxState,
    month: u32,
) -> Result<(), SimulationError> {
    for distribution in &fixture.scenario.distributions {
        let pool_lots: Vec<_> = lots
            .iter()
            .filter(|lot| {
                lot.spec.agent_id == distribution.agent_id
                    && lot.spec.account_id == distribution.holding_account_id
                    && lot.spec.asset_id == distribution.asset_id
            })
            .collect();
        // An enabled purchase policy can name a pool before its first purchase.
        let scale = pool_lots.first().map_or(1, |lot| lot.spec.quantity_scale);
        let units = pool_lots.iter().try_fold(0_i64, |total, lot| {
            total
                .checked_add(lot.units_remaining.0)
                .ok_or(ArithmeticError::Overflow {
                    operation: "distribution pool quantity",
                })
        })?;
        let per_unit = series_value(
            fixture,
            &format!("security_distribution:{}", distribution.asset_id),
            rollout_id,
            month,
        )?;
        // A rate, not a price: `security_distribution:` is quoted in nano-quanta per unit, so
        // that a payout far below one quantum per unit survives to be multiplied by the
        // position rather than rounding to zero first (#5832).
        let total_amount = PerUnitRate(per_unit)
            .times(Units::new(Quantity(units), scale), "security distribution")?;
        for (slice_index, slice) in distribution.tax_character.iter().enumerate() {
            let amount = total_amount.scaled_by(
                Factor::parts_per_billion(slice.fraction_ppb),
                "security distribution tax slice",
            )?;
            let cause_id = format!(
                "distribution:{}:{}:s{slice_index}:m{month}",
                distribution.agent_id, distribution.asset_id
            );
            transfer_money(
                ledger,
                recorder,
                month,
                &cause_id,
                &AccountRef::new(EXTERNAL_AGENT, "boundary"),
                &AccountRef::new(&distribution.agent_id, &distribution.to_account_id),
                amount,
            )?;
            record_interest_income(
                tax,
                &distribution.agent_id,
                slice.issuer_jurisdiction_id.as_deref(),
                amount,
            )?;
            recorder.record_distribution(DistributionOutcome {
                month,
                agent_id: distribution.agent_id.clone(),
                holding_account_id: distribution.holding_account_id.clone(),
                asset_id: distribution.asset_id.clone(),
                slice_index: u32::try_from(slice_index).map_err(|_| ArithmeticError::Overflow {
                    operation: "distribution slice index",
                })?,
                fraction_ppb: slice.fraction_ppb,
                issuer_jurisdiction_id: slice.issuer_jurisdiction_id.clone(),
                units: Quantity(units),
                amount,
            })?;
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
/// The scheduled-sale convention chooses FIFO, then submits those exact holdings.
pub(super) fn execute_sale(
    fixture: &ExecutionInput,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    tax: &mut TaxState,
    scheduled_tlh: &mut ScheduledTlhGiveBack,
    sale: &crate::execution::ScheduledSaleSpec,
) -> Result<(), SimulationError> {
    let candidates: Vec<usize> = lots
        .iter()
        .enumerate()
        .filter(|(_, lot)| {
            lot.spec.agent_id == sale.agent_id
                && lot.spec.account_id == sale.account_id
                && lot.spec.asset_id == sale.asset_id
                && lot.units_remaining.0 > 0
        })
        .map(|(index, _)| index)
        .collect();
    if candidates.is_empty() {
        return Err(SimulationError::MissingSalePool {
            cause_id: sale.cause_id.clone(),
            agent_id: sale.agent_id.clone(),
            account_id: sale.account_id.clone(),
            asset_id: sale.asset_id.clone(),
        });
    }
    let request = SaleRequest {
        cause_id: sale.cause_id.clone(),
        agent_id: sale.agent_id.clone(),
        proceeds_account_id: sale.proceeds_account_id.clone(),
        asset_id: sale.asset_id.clone(),
        lots: select_fifo(lots, &candidates, sale.units, &sale.cause_id)?,
    };
    let price = PerUnit(series_value(
        fixture,
        &format!("security:{}", sale.asset_id),
        rollout_id,
        sale.month,
    )?);
    execute_lot_sale(
        fixture,
        ledger,
        recorder,
        lots,
        tax,
        SaleTlh::Scheduled(scheduled_tlh),
        sale.month,
        SaleProceeds::Quoted(price),
        &request,
    )
}

pub(super) fn series_value(
    fixture: &ExecutionInput,
    series_id: &str,
    rollout: u32,
    snapshot: u32,
) -> Result<i64, SimulationError> {
    let series: &SeriesSpec = fixture
        .series
        .iter()
        .find(|series| series.series_id == series_id)
        .ok_or_else(|| SimulationError::MissingSeries {
            series_id: series_id.into(),
        })?;
    series
        .value(rollout, snapshot)
        .ok_or_else(|| SimulationError::MissingSeriesValue {
            series_id: series_id.into(),
            rollout,
            snapshot,
        })
}

pub(super) fn amount_value(
    fixture: &ExecutionInput,
    rollout: u32,
    month: u32,
    amount: &AmountSpec,
) -> Result<Money, SimulationError> {
    match amount {
        AmountSpec::Fixed(amount) => Ok(*amount),
        AmountSpec::FixedSchedule(amount) => Ok(amount.amount),
        AmountSpec::SeriesIndexed(amount) => {
            let elapsed = month - amount.base_month_index;
            let reset_month = amount.base_month_index
                + (elapsed / amount.adjustment_period_months) * amount.adjustment_period_months;
            let base_level =
                series_value(fixture, &amount.series_id, rollout, amount.base_month_index)?;
            let reset_level = series_value(fixture, &amount.series_id, rollout, reset_month)?;
            Ok(amount.base_amount.scaled_by(
                Factor::new(reset_level, base_level),
                "series-indexed amount",
            )?)
        }
    }
}

fn bond_is_active(bond: &BondSpec, snapshot_month: u32) -> bool {
    i64::from(bond.purchase_month_index) <= i64::from(snapshot_month)
        && i64::from(snapshot_month) < i64::from(bond.maturity_month_index)
}

fn bond_pays(bond: &BondSpec, month: u32) -> bool {
    let elapsed = i64::from(month) - i64::from(bond.purchase_month_index);
    elapsed > 0
        && i64::from(month) <= i64::from(bond.maturity_month_index)
        && elapsed % i64::from(bond.coupon_period_months) == 0
}

fn bond_principal(
    fixture: &ExecutionInput,
    rollout_id: u32,
    bond: &BondSpec,
    snapshot_month: u32,
) -> Result<Money, SimulationError> {
    if !bond.inflation_indexed {
        return Ok(bond.face_value);
    }
    let base_month = bond.purchase_month_index.max(0) as u32;
    let base_level = series_value(fixture, "inflation", rollout_id, base_month)?;
    let level = series_value(fixture, "inflation", rollout_id, snapshot_month)?;
    Ok(bond
        .face_value
        .scaled_by(Factor::new(level, base_level), "bond indexed principal")?)
}

pub(super) fn bond_period_rate_ppb(bond: &BondSpec) -> Result<i64, SimulationError> {
    mul_div_round_half_up(
        bond.annual_coupon_rate_ppb,
        i64::from(bond.coupon_period_months),
        12,
        "bond period rate",
    )
    .map_err(Into::into)
}

pub(super) fn bond_coupon(principal: Money, bond: &BondSpec) -> Result<Money, SimulationError> {
    let coupon = if bond.inflation_indexed {
        i128::from(mul_div_round_half_up(
            principal.0,
            bond_period_rate_ppb(bond)?,
            WIRE_RATE_SCALE,
            "indexed bond coupon",
        )?)
    } else {
        let rate_times_period = i128::from(bond.annual_coupon_rate_ppb)
            .checked_mul(i128::from(bond.coupon_period_months))
            .ok_or(ArithmeticError::Overflow {
                operation: "nominal bond coupon rate",
            })?;
        mul_div_i128_round_half_up(
            i128::from(principal.0),
            rate_times_period,
            i128::from(WIRE_RATE_SCALE) * 12,
            "nominal bond coupon",
        )?
    };
    Ok(Money(i64::try_from(coupon).map_err(|_| {
        ArithmeticError::Overflow {
            operation: "bond coupon",
        }
    })?))
}

pub(super) fn bond_states(
    fixture: &ExecutionInput,
    rollout_id: u32,
    snapshot_month: u32,
) -> Result<Vec<BondState>, SimulationError> {
    fixture
        .scenario
        .initial_bonds
        .iter()
        .map(|bond| {
            let active = bond_is_active(bond, snapshot_month);
            Ok(BondState {
                bond_id: bond.bond_id.clone(),
                agent_id: bond.agent_id.clone(),
                account_id: bond.account_id.clone(),
                principal: if active {
                    bond_principal(fixture, rollout_id, bond, snapshot_month)?
                } else {
                    Money(0)
                },
                active,
            })
        })
        .collect()
}

pub(super) fn execute_bonds(
    fixture: &ExecutionInput,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax: &mut TaxState,
    month: u32,
) -> Result<(), SimulationError> {
    for bond in &fixture.scenario.initial_bonds {
        let principal = bond_principal(fixture, rollout_id, bond, month)?;
        let coupon = if bond_pays(bond, month) {
            bond_coupon(principal, bond)?
        } else {
            Money(0)
        };
        let redemption = if i64::from(month) == i64::from(bond.maturity_month_index) {
            if bond.inflation_indexed {
                Money(principal.0.max(bond.face_value.0))
            } else {
                bond.face_value
            }
        } else {
            Money(0)
        };
        let accretion = if bond.inflation_indexed && month > 0 && bond_is_active(bond, month) {
            principal.checked_sub(bond_principal(fixture, rollout_id, bond, month - 1)?)?
        } else {
            Money(0)
        };
        let paid = coupon.checked_add(redemption)?;
        if paid != Money(0) {
            let cause_id = format!("bond:{}:m{month}", bond.bond_id);
            transfer_money(
                ledger,
                recorder,
                month,
                &cause_id,
                &AccountRef::new(EXTERNAL_AGENT, "boundary"),
                &AccountRef::new(&bond.agent_id, &bond.account_id),
                paid,
            )?;
        }
        let income = coupon.checked_add(accretion)?;
        if income != Money(0) {
            record_interest_income(
                tax,
                &bond.agent_id,
                bond.issuer_jurisdiction_id.as_deref(),
                income,
            )?;
        }
        if coupon != Money(0) || accretion != Money(0) || redemption != Money(0) {
            recorder.record_bond_cashflow(BondCashflowOutcome {
                month,
                cause_id: format!("bond:{}:m{month}", bond.bond_id),
                bond_id: bond.bond_id.clone(),
                agent_id: bond.agent_id.clone(),
                account_id: bond.account_id.clone(),
                issuer_jurisdiction_id: bond.issuer_jurisdiction_id.clone(),
                coupon,
                accretion,
                redemption,
                principal,
            })?;
        }
    }
    Ok(())
}
