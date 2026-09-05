//! Security lots and their prices: distributions, FIFO sales, and held-to-maturity
//! nominal bonds and TIPS.

use super::*;

#[derive(Clone, Debug)]
pub(super) struct LotState {
    pub(super) spec: InitialLotSpec,
    pub(super) fifo_rank: i64,
    pub(super) units_remaining: Quantity,
    pub(super) basis_remaining: Money,
    pub(super) basis_per_unit: Money,
}

#[derive(Clone, Debug)]
pub(super) struct PlannedDisposition {
    pub(super) lot_index: usize,
    pub(super) units: Quantity,
    pub(super) basis: Money,
    pub(super) proceeds: Money,
    pub(super) realized_gain: Money,
}

pub(super) fn canonical_lot_asset_id(asset_id: &str) -> String {
    if private_equity_issuer(asset_id).is_some() {
        asset_id.to_owned()
    } else {
        format!("security:{asset_id}")
    }
}

pub(super) fn execute_distributions(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &[LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
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
        let scale = pool_lots[0].spec.quantity_scale;
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
        let total_amount = Money(mul_div_round_half_up(
            per_unit,
            units,
            scale,
            "security distribution",
        )?);
        for (slice_index, slice) in distribution.tax_character.iter().enumerate() {
            let amount = Money(mul_div_round_half_up(
                total_amount.0,
                slice.fraction_ppb,
                RATE_SCALE,
                "security distribution tax slice",
            )?);
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
                fixture,
                tax_facts,
                &distribution.agent_id,
                slice.issuer_jurisdiction_id.as_deref(),
                jurisdiction_level(fixture, slice.issuer_jurisdiction_id.as_deref()),
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
pub(super) fn execute_sale(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    scheduled_tlh: &mut ScheduledTlhGiveBack,
    sale: &crate::fixture::ScheduledSaleSpec,
) -> Result<(), SimulationError> {
    let mut candidates: Vec<usize> = lots
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
    candidates.sort_by_key(|index| (lots[*index].fifo_rank, lots[*index].spec.lot_id.clone()));
    let available = candidates.iter().try_fold(0_i64, |total, index| {
        total
            .checked_add(lots[*index].units_remaining.0)
            .ok_or(ArithmeticError::Overflow {
                operation: "sale pool quantity",
            })
    })?;
    if sale.units.0 > available {
        return Err(SimulationError::InsufficientLotUnits {
            cause_id: sale.cause_id.clone(),
            requested: sale.units.0,
            available,
        });
    }
    let series_id = format!("security:{}", sale.asset_id);
    let price = Money(series_value(fixture, &series_id, rollout_id, sale.month)?);
    let mut remaining = sale.units.0;
    let mut planned = Vec::new();
    let mut total_proceeds = Money(0);
    let mut total_gain = Money(0);
    for index in candidates {
        if remaining == 0 {
            break;
        }
        let lot = &lots[index];
        let units = remaining.min(lot.units_remaining.0);
        let basis = Money(mul_div_round_half_up(
            lot.basis_per_unit.0,
            units,
            lot.spec.quantity_scale,
            "FIFO basis allocation",
        )?);
        let proceeds = Money(mul_div_round_half_up(
            price.0,
            units,
            lot.spec.quantity_scale,
            "sale proceeds",
        )?);
        let realized_gain = proceeds.checked_sub(basis)?;
        total_proceeds = total_proceeds.checked_add(proceeds)?;
        total_gain = total_gain.checked_add(realized_gain)?;
        planned.push(PlannedDisposition {
            lot_index: index,
            units: Quantity(units),
            basis,
            proceeds,
            realized_gain,
        });
        remaining -= units;
    }
    debug_assert_eq!(remaining, 0);
    let tlh_give_back = tlh_give_back_for_scheduled_sale(fixture, lots, &planned, scheduled_tlh)?;

    let mut postings = Vec::with_capacity(planned.len() + 2);
    postings.push(Posting {
        account: AccountRef::new(&sale.agent_id, &sale.proceeds_account_id),
        amount: total_proceeds,
    });
    for item in &planned {
        postings.push(Posting {
            account: asset_basis_account(&lots[item.lot_index].spec),
            amount: item.basis.checked_neg()?,
        });
    }
    postings.push(Posting {
        account: realized_gain_account(&sale.agent_id),
        amount: total_gain.checked_neg()?,
    });
    recorder.apply_entry(
        ledger,
        JournalEntry {
            month: sale.month,
            cause_id: sale.cause_id.clone(),
            postings,
        },
    )?;

    for (item, give_back) in planned.into_iter().zip(tlh_give_back) {
        let lot = &mut lots[item.lot_index];
        let long_term = i64::from(sale.month) - i64::from(lot.spec.purchase_month) >= 12;
        lot.units_remaining.0 -= item.units.0;
        lot.basis_remaining = lot.basis_remaining.checked_sub(item.basis)?;
        record_capital_gain(
            tax_facts,
            &sale.agent_id,
            item.realized_gain.checked_add(give_back)?,
            long_term,
        )?;
        recorder.record_disposition(LotDisposition {
            month: sale.month,
            cause_id: sale.cause_id.clone(),
            agent_id: lot.spec.agent_id.clone(),
            source_account_id: lot.spec.account_id.clone(),
            asset_id: canonical_lot_asset_id(&lot.spec.asset_id),
            lot_id: lot.spec.lot_id.clone(),
            purchase_month: lot.spec.purchase_month,
            quantity_scale: lot.spec.quantity_scale,
            units: item.units,
            basis: item.basis,
            proceeds: item.proceeds,
            proceeds_account_id: sale.proceeds_account_id.clone(),
            realized_gain: item.realized_gain,
        })?;
    }
    Ok(())
}

pub(super) fn series_value(
    fixture: &Fixture,
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
    fixture: &Fixture,
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
            Ok(Money(mul_div_round_half_up(
                amount.base_amount.0,
                reset_level,
                base_level,
                "series-indexed amount",
            )?))
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
    fixture: &Fixture,
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
    Ok(Money(mul_div_round_half_up(
        bond.face_value.0,
        level,
        base_level,
        "bond indexed principal",
    )?))
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
            RATE_SCALE,
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
            i128::from(RATE_SCALE) * 12,
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
    fixture: &Fixture,
    rollout_id: u32,
    snapshot_month: u32,
    failed: bool,
) -> Result<Vec<BondState>, SimulationError> {
    fixture
        .scenario
        .initial_bonds
        .iter()
        .map(|bond| {
            let active = !failed && bond_is_active(bond, snapshot_month);
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
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
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
                fixture,
                tax_facts,
                &bond.agent_id,
                bond.issuer_jurisdiction_id.as_deref(),
                jurisdiction_level(fixture, bond.issuer_jurisdiction_id.as_deref()),
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
