//! Fixture validation: every structural and numeric precondition the engine relies on,
//! checked once before any rollout executes.

use super::*;

pub(super) fn validate_fixture(fixture: &Fixture) -> Result<(), SimulationError> {
    if fixture.schema_version != FIXTURE_SCHEMA_VERSION {
        return Err(SimulationError::SchemaVersion {
            actual: fixture.schema_version,
            expected: FIXTURE_SCHEMA_VERSION,
        });
    }
    if fixture.rollout_count == 0 {
        return Err(SimulationError::EmptyRollouts);
    }
    if fixture.scenario.horizon_months == 0 {
        return Err(SimulationError::EmptyHorizon);
    }
    if fixture.currency_code.len() != 3
        || !fixture
            .currency_code
            .bytes()
            .all(|byte| byte.is_ascii_uppercase())
    {
        return Err(SimulationError::InvalidCurrencyCode {
            currency_code: fixture.currency_code.clone(),
        });
    }
    if !is_positive_decimal(&fixture.currency_quantum) {
        return Err(SimulationError::InvalidCurrencyQuantum {
            currency_quantum: fixture.currency_quantum.clone(),
        });
    }
    let snapshots = fixture
        .scenario
        .horizon_months
        .checked_add(1)
        .ok_or(SimulationError::FixtureDimensions)?;
    let expected = usize::try_from(u64::from(fixture.rollout_count) * u64::from(snapshots))
        .map_err(|_| SimulationError::FixtureDimensions)?;
    let mut series_ids = BTreeSet::new();
    for series in &fixture.series {
        validate_identifier("series", &series.series_id)?;
        if !series_ids.insert(series.series_id.clone()) {
            return Err(SimulationError::DuplicateSeries {
                series_id: series.series_id.clone(),
            });
        }
        if series.snapshots != snapshots || series.values.len() != expected {
            return Err(SimulationError::SeriesShape {
                series_id: series.series_id.clone(),
                actual: series.values.len(),
                expected,
            });
        }
        if (series.series_id.starts_with("security:")
            || series.series_id.starts_with("security_distribution:")
            || series.series_id.starts_with("home_value:"))
            && let Some((index, value)) = series
                .values
                .iter()
                .copied()
                .enumerate()
                .find(|(_, value)| *value <= 0)
        {
            return Err(SimulationError::InvalidSecurityPrice {
                series_id: series.series_id.clone(),
                index,
                value,
            });
        }
    }

    let mut accounts = BTreeSet::new();
    let mut agents = BTreeSet::new();
    for account in &fixture.scenario.accounts {
        agents.insert(account.account.agent_id.clone());
        if !accounts.insert(account.account.clone()) {
            return Err(SimulationError::DuplicateAccount {
                agent_id: account.account.agent_id.clone(),
                account_id: account.account.account_id.clone(),
            });
        }
    }
    let mut jurisdiction_levels = BTreeMap::new();
    for jurisdiction in &fixture.scenario.jurisdictions {
        validate_identifier("jurisdiction", &jurisdiction.jurisdiction_id)?;
        if jurisdiction_levels
            .insert(jurisdiction.jurisdiction_id.clone(), jurisdiction.level)
            .is_some()
        {
            return Err(SimulationError::DuplicateJurisdictionIdentity {
                jurisdiction_id: jurisdiction.jurisdiction_id.clone(),
            });
        }
    }
    for transfer in &fixture.scenario.scheduled_transfers {
        validate_identifier("scheduled transfer", &transfer.cause_id)?;
        validate_event_month(
            "scheduled transfer",
            &transfer.cause_id,
            transfer.month,
            fixture.scenario.horizon_months,
        )?;
        validate_amount_spec(
            fixture,
            "scheduled transfer",
            &transfer.cause_id,
            &transfer.amount,
            std::iter::once(transfer.month),
        )?;
        validate_income_category(transfer.income_category.as_deref())?;
        validate_income_category(transfer.deduction_category.as_deref())?;
        validate_account(&accounts, &transfer.from, &transfer.cause_id)?;
        validate_account(&accounts, &transfer.to, &transfer.cause_id)?;
    }
    for transfer in &fixture.scenario.recurring_transfers {
        validate_identifier("recurring transfer", &transfer.cause_id)?;
        validate_event_month(
            "recurring transfer",
            &transfer.cause_id,
            transfer.start_month,
            fixture.scenario.horizon_months,
        )?;
        if let Some(end_month) = transfer.end_month
            && end_month < transfer.start_month
        {
            return Err(SimulationError::InvalidRecurringRange {
                kind: "recurring transfer",
                cause_id: transfer.cause_id.clone(),
                start_month: transfer.start_month,
                end_month,
            });
        }
        validate_amount_spec(
            fixture,
            "recurring transfer",
            &transfer.cause_id,
            &transfer.amount,
            transfer.start_month
                ..=transfer
                    .end_month
                    .unwrap_or(fixture.scenario.horizon_months - 1)
                    .min(fixture.scenario.horizon_months - 1),
        )?;
        validate_income_category(transfer.income_category.as_deref())?;
        validate_income_category(transfer.deduction_category.as_deref())?;
        validate_account(&accounts, &transfer.from, &transfer.cause_id)?;
        validate_account(&accounts, &transfer.to, &transfer.cause_id)?;
    }
    for obligation in &fixture.scenario.obligations {
        validate_identifier("obligation", &obligation.obligation_id)?;
        validate_identifier("obligation type", &obligation.obligation_type)?;
        validate_event_month(
            "obligation",
            &obligation.obligation_id,
            obligation.month,
            fixture.scenario.horizon_months,
        )?;
        validate_amount_spec(
            fixture,
            "obligation",
            &obligation.obligation_id,
            &obligation.amount_due,
            std::iter::once(obligation.month),
        )?;
        validate_income_category(obligation.deduction_category.as_deref())?;
        validate_account(&accounts, &obligation.from, &obligation.obligation_id)?;
        validate_account(&accounts, &obligation.to, &obligation.obligation_id)?;
    }
    for obligation in &fixture.scenario.recurring_obligations {
        validate_identifier("recurring obligation", &obligation.obligation_id)?;
        validate_identifier("obligation type", &obligation.obligation_type)?;
        validate_event_month(
            "recurring obligation",
            &obligation.obligation_id,
            obligation.start_month,
            fixture.scenario.horizon_months,
        )?;
        if let Some(end_month) = obligation.end_month
            && end_month < obligation.start_month
        {
            return Err(SimulationError::InvalidRecurringRange {
                kind: "recurring obligation",
                cause_id: obligation.obligation_id.clone(),
                start_month: obligation.start_month,
                end_month,
            });
        }
        validate_amount_spec(
            fixture,
            "recurring obligation",
            &obligation.obligation_id,
            &obligation.amount_due,
            obligation.start_month
                ..=obligation
                    .end_month
                    .unwrap_or(fixture.scenario.horizon_months - 1)
                    .min(fixture.scenario.horizon_months - 1),
        )?;
        validate_income_category(obligation.deduction_category.as_deref())?;
        validate_account(&accounts, &obligation.from, &obligation.obligation_id)?;
        validate_account(&accounts, &obligation.to, &obligation.obligation_id)?;
    }

    let mut lots = BTreeSet::new();
    let mut pool_scales = BTreeMap::new();
    for lot in &fixture.scenario.initial_lots {
        validate_identifier("lot", &lot.lot_id)?;
        validate_identifier("asset", &lot.asset_id)?;
        if lot.quantity_scale <= 0 {
            return Err(SimulationError::InvalidQuantityScale {
                lot_id: lot.lot_id.clone(),
                quantity_scale: lot.quantity_scale,
            });
        }
        if lot.units.0 <= 0 || lot.basis.0 < 0 {
            return Err(SimulationError::InvalidLot {
                lot_id: lot.lot_id.clone(),
                units: lot.units.0,
                basis: lot.basis.0,
            });
        }
        if i128::from(lot.basis.0) * i128::from(lot.quantity_scale) % i128::from(lot.units.0) != 0 {
            return Err(SimulationError::InexactLotBasis {
                lot_id: lot.lot_id.clone(),
            });
        }
        if !lots.insert(lot.lot_id.clone()) {
            return Err(SimulationError::DuplicateLot {
                lot_id: lot.lot_id.clone(),
            });
        }
        let pool = (
            lot.agent_id.clone(),
            lot.account_id.clone(),
            lot.asset_id.clone(),
        );
        if let Some(first_scale) = pool_scales.insert(pool, lot.quantity_scale)
            && first_scale != lot.quantity_scale
        {
            return Err(SimulationError::MixedQuantityScale {
                agent_id: lot.agent_id.clone(),
                account_id: lot.account_id.clone(),
                asset_id: lot.asset_id.clone(),
                first_scale,
                second_scale: lot.quantity_scale,
            });
        }
        if !agents.contains(&lot.agent_id) {
            return Err(SimulationError::UnknownAccountReference {
                context: format!("lot {:?}", lot.lot_id),
                agent_id: lot.agent_id.clone(),
                account_id: lot.account_id.clone(),
            });
        }
    }
    let mut private_equity_issuers = BTreeMap::<String, String>::new();
    for lot in &fixture.scenario.initial_lots {
        let Some(issuer_id) = private_equity_issuer(&lot.asset_id) else {
            continue;
        };
        if let Some(first_owner) =
            private_equity_issuers.insert(issuer_id.to_owned(), lot.agent_id.clone())
            && first_owner != lot.agent_id
        {
            return Err(SimulationError::MixedPrivateEquityOwners {
                issuer_id: issuer_id.to_owned(),
            });
        }
    }
    let mut pe_policy_owners = BTreeSet::new();
    for policy in &fixture.scenario.private_equity_tender_policies {
        if !agents.contains(&policy.owner_agent_id)
            || !pe_policy_owners.insert(policy.owner_agent_id.clone())
        {
            return Err(SimulationError::InvalidPrivateEquityPolicy {
                owner_agent_id: policy.owner_agent_id.clone(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&policy.owner_agent_id, &policy.proceeds_account_id),
            "private-equity proceeds",
        )?;
        validate_amount_spec(
            fixture,
            "private-equity policy",
            &policy.owner_agent_id,
            &policy.liquid_net_worth_floor,
            0..fixture.scenario.horizon_months,
        )?;
    }
    for issuer_id in private_equity_issuers.keys() {
        validate_private_equity_channels(fixture, issuer_id)?;
    }
    for (policy_index, policy) in fixture.scenario.harvest_policies.iter().enumerate() {
        let valid = agents.contains(&policy.owner_agent_id)
            && policy.peak_annual_yield_ppb > 0
            && policy.floor_annual_yield_ppb >= 0
            && policy.floor_annual_yield_ppb <= policy.peak_annual_yield_ppb
            && policy.maturity_decay_exponent_ppb > 0
            && policy.maturity_decay_exponent_ppb % (RATE_SCALE_PPB / 2) == 0
            && policy.drawdown_sensitivity_ppb >= 0
            && (0..=RATE_SCALE_PPB).contains(&policy.short_term_fraction_ppb)
            && private_equity_issuer(&policy.asset_id).is_none()
            && fixture
                .series
                .iter()
                .any(|series| series.series_id == format!("security:{}", policy.asset_id));
        if !valid {
            return Err(SimulationError::InvalidHarvestPolicy { policy_index });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&policy.owner_agent_id, &policy.account_id),
            "harvest policy",
        )?;
    }
    for policy in &fixture.scenario.target_allocation_policies {
        if policy.purchase_slots_per_sleeve == 0 {
            continue;
        }
        let account_id = policy
            .source_account_ids
            .first()
            .unwrap_or(&policy.account_id);
        for sleeve in &policy.sleeves {
            let pool = (
                policy.agent_id.clone(),
                account_id.clone(),
                sleeve.asset_id.clone(),
            );
            if let Some(first_scale) = pool_scales.insert(pool, sleeve.quantity_scale)
                && first_scale != sleeve.quantity_scale
            {
                return Err(SimulationError::MixedQuantityScale {
                    agent_id: policy.agent_id.clone(),
                    account_id: account_id.clone(),
                    asset_id: sleeve.asset_id.clone(),
                    first_scale,
                    second_scale: sleeve.quantity_scale,
                });
            }
        }
    }
    let mut bonds = BTreeSet::new();
    for bond in &fixture.scenario.initial_bonds {
        validate_identifier("bond", &bond.bond_id)?;
        if !bonds.insert(bond.bond_id.clone()) {
            return Err(SimulationError::DuplicateBond {
                bond_id: bond.bond_id.clone(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&bond.agent_id, &bond.account_id),
            &format!("bond {:?}", bond.bond_id),
        )?;
        if let Some(issuer) = &bond.issuer_jurisdiction_id
            && !jurisdiction_levels.contains_key(issuer)
        {
            return Err(SimulationError::UnknownBondIssuer {
                bond_id: bond.bond_id.clone(),
                jurisdiction_id: issuer.clone(),
            });
        }
        let Some(term_months) = bond
            .maturity_month_index
            .checked_sub(bond.purchase_month_index)
        else {
            return Err(SimulationError::InvalidBondTerms {
                bond_id: bond.bond_id.clone(),
            });
        };
        if bond.face_value.0 <= 0
            || bond.purchase_price != bond.face_value
            || bond.annual_coupon_rate_ppb < 0
            || bond.coupon_period_months == 0
            || term_months <= 0
            || i64::from(term_months) % i64::from(bond.coupon_period_months) != 0
        {
            return Err(SimulationError::InvalidBondTerms {
                bond_id: bond.bond_id.clone(),
            });
        }
        let reconstructed_rate = ((bond.annual_coupon_rate_ppb as f64 / RATE_SCALE as f64)
            * RATE_SCALE as f64)
            .round() as i64;
        if bond.annual_coupon_rate_ppb > MAX_EXACT_F64_INTEGER
            || reconstructed_rate != bond.annual_coupon_rate_ppb
        {
            return Err(SimulationError::InexactBondCouponRate {
                bond_id: bond.bond_id.clone(),
                rate_ppb: bond.annual_coupon_rate_ppb,
            });
        }
        if bond.inflation_indexed {
            let exact_period_rate = bond_period_rate_ppb(bond)?;
            let legacy_period_rate = (((bond.annual_coupon_rate_ppb as f64 / RATE_SCALE as f64)
                * f64::from(bond.coupon_period_months)
                / 12.0
                * RATE_SCALE as f64)
                + 0.5)
                .floor() as i64;
            if exact_period_rate != legacy_period_rate {
                return Err(SimulationError::InexactBondPeriodRate {
                    bond_id: bond.bond_id.clone(),
                });
            }
            if !series_ids.contains("inflation") {
                return Err(SimulationError::MissingBondInflationSeries {
                    bond_id: bond.bond_id.clone(),
                });
            }
            let base_month = bond.purchase_month_index.max(0) as u32;
            if base_month > fixture.scenario.horizon_months {
                return Err(SimulationError::InvalidBondTerms {
                    bond_id: bond.bond_id.clone(),
                });
            }
            for rollout in 0..fixture.rollout_count {
                for snapshot in 0..=fixture.scenario.horizon_months {
                    let level = fixture
                        .series
                        .iter()
                        .find(|series| series.series_id == "inflation")
                        .and_then(|series| series.value(rollout, snapshot))
                        .ok_or_else(|| SimulationError::MissingSeriesValue {
                            series_id: "inflation".into(),
                            rollout,
                            snapshot,
                        })?;
                    validate_amount_index_level(
                        &bond.bond_id,
                        "inflation",
                        rollout,
                        snapshot,
                        level,
                    )?;
                }
            }
        }
    }
    for sale in &fixture.scenario.scheduled_sales {
        validate_identifier("sale", &sale.cause_id)?;
        validate_identifier("asset", &sale.asset_id)?;
        validate_event_month(
            "sale",
            &sale.cause_id,
            sale.month,
            fixture.scenario.horizon_months,
        )?;
        if sale.units.0 <= 0 {
            return Err(SimulationError::InvalidSaleUnits {
                cause_id: sale.cause_id.clone(),
                units: sale.units.0,
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&sale.agent_id, &sale.proceeds_account_id),
            &sale.cause_id,
        )?;
        if !pool_scales.contains_key(&(
            sale.agent_id.clone(),
            sale.account_id.clone(),
            sale.asset_id.clone(),
        )) {
            return Err(SimulationError::MissingSalePool {
                cause_id: sale.cause_id.clone(),
                agent_id: sale.agent_id.clone(),
                account_id: sale.account_id.clone(),
                asset_id: sale.asset_id.clone(),
            });
        }
        let series_id = format!("security:{}", sale.asset_id);
        if !series_ids.contains(&series_id) {
            return Err(SimulationError::MissingSeries { series_id });
        }
    }
    let mut distributions = BTreeSet::new();
    for distribution in &fixture.scenario.distributions {
        validate_identifier("distribution asset", &distribution.asset_id)?;
        let pool = (
            distribution.agent_id.clone(),
            distribution.holding_account_id.clone(),
            distribution.asset_id.clone(),
        );
        if !distributions.insert(pool.clone()) {
            return Err(SimulationError::DuplicateDistribution {
                agent_id: pool.0,
                account_id: pool.1,
                asset_id: pool.2,
            });
        }
        if !pool_scales.contains_key(&pool) {
            return Err(SimulationError::MissingDistributionPool {
                agent_id: pool.0,
                account_id: pool.1,
                asset_id: pool.2,
            });
        }
        let fraction_total = distribution
            .tax_character
            .iter()
            .map(|slice| i128::from(slice.fraction_ppb))
            .sum::<i128>();
        if distribution.tax_character.is_empty()
            || distribution.tax_character.iter().any(|slice| {
                let reconstructed = ((slice.fraction_ppb as f64 / RATE_SCALE as f64)
                    * RATE_SCALE as f64)
                    .round() as i64;
                slice.fraction_ppb <= 0 || reconstructed != slice.fraction_ppb
            })
            || fraction_total != i128::from(RATE_SCALE)
        {
            return Err(SimulationError::InvalidDistributionTaxCharacter {
                agent_id: distribution.agent_id.clone(),
                account_id: distribution.holding_account_id.clone(),
                asset_id: distribution.asset_id.clone(),
            });
        }
        for slice in &distribution.tax_character {
            if let Some(issuer) = &slice.issuer_jurisdiction_id
                && !jurisdiction_levels.contains_key(issuer)
            {
                return Err(SimulationError::UnknownDistributionIssuer {
                    agent_id: distribution.agent_id.clone(),
                    account_id: distribution.holding_account_id.clone(),
                    asset_id: distribution.asset_id.clone(),
                    jurisdiction_id: issuer.clone(),
                });
            }
        }
        validate_account(
            &accounts,
            &AccountRef::new(&distribution.agent_id, &distribution.to_account_id),
            "distribution destination",
        )?;
        let series_id = format!("security_distribution:{}", distribution.asset_id);
        if !series_ids.contains(&series_id) {
            return Err(SimulationError::MissingSeries { series_id });
        }
    }
    let mut target_allocation_accounts = BTreeSet::new();
    for (policy_index, policy) in fixture
        .scenario
        .target_allocation_policies
        .iter()
        .enumerate()
    {
        validate_identifier("target-allocation cause", &policy.cause_id_prefix)?;
        validate_account(
            &accounts,
            &AccountRef::new(&policy.agent_id, &policy.account_id),
            &policy.cause_id_prefix,
        )?;
        if !target_allocation_accounts.insert((policy.agent_id.clone(), policy.account_id.clone()))
        {
            return Err(SimulationError::DuplicateTargetAllocationPolicy {
                agent_id: policy.agent_id.clone(),
                account_id: policy.account_id.clone(),
            });
        }
        if let Some(tolerance) = policy.rebalance_tolerance_ppb {
            let reconstructed =
                ((tolerance as f64 / RATE_SCALE as f64) * RATE_SCALE as f64).round() as i64;
            if !(0..=MAX_EXACT_F64_INTEGER).contains(&tolerance)
                || reconstructed != tolerance
                || policy.purchase_slots_per_sleeve == 0
            {
                return Err(SimulationError::InvalidTargetAllocationPolicy {
                    agent_id: policy.agent_id.clone(),
                    account_id: policy.account_id.clone(),
                });
            }
        }
        if policy.sleeves.is_empty()
            || policy.cash_floor.base_amount().0 < 0
            || policy.cash_floor.base_amount().0 > policy.cash_ceiling.base_amount().0
        {
            return Err(SimulationError::InvalidTargetAllocationPolicy {
                agent_id: policy.agent_id.clone(),
                account_id: policy.account_id.clone(),
            });
        }
        validate_amount_spec(
            fixture,
            "target-allocation floor",
            &policy.cause_id_prefix,
            &policy.cash_floor,
            0..fixture.scenario.horizon_months,
        )?;
        validate_amount_spec(
            fixture,
            "target-allocation ceiling",
            &policy.cause_id_prefix,
            &policy.cash_ceiling,
            0..fixture.scenario.horizon_months,
        )?;
        let sources = if policy.source_account_ids.is_empty() {
            vec![policy.account_id.as_str()]
        } else {
            policy
                .source_account_ids
                .iter()
                .map(String::as_str)
                .collect()
        };
        if sources.iter().copied().collect::<BTreeSet<_>>().len() != sources.len() {
            return Err(SimulationError::InvalidTargetAllocationPolicy {
                agent_id: policy.agent_id.clone(),
                account_id: policy.account_id.clone(),
            });
        }
        let mut assets = BTreeSet::new();
        for (sleeve_index, sleeve) in policy.sleeves.iter().enumerate() {
            validate_identifier("target-allocation asset", &sleeve.asset_id)?;
            if sleeve.weight <= 0 || sleeve.quantity_scale <= 0 {
                return Err(SimulationError::InvalidTargetAllocationPolicy {
                    agent_id: policy.agent_id.clone(),
                    account_id: policy.account_id.clone(),
                });
            }
            if !assets.insert(sleeve.asset_id.clone()) {
                return Err(SimulationError::DuplicateTargetAllocationSleeve {
                    agent_id: policy.agent_id.clone(),
                    account_id: policy.account_id.clone(),
                    asset_id: sleeve.asset_id.clone(),
                });
            }
            let scales: BTreeSet<_> = sources
                .iter()
                .filter_map(|source| {
                    pool_scales
                        .get(&(
                            policy.agent_id.clone(),
                            (*source).to_owned(),
                            sleeve.asset_id.clone(),
                        ))
                        .copied()
                })
                .collect();
            if scales.len() > 1
                || scales
                    .iter()
                    .next()
                    .is_some_and(|scale| *scale != sleeve.quantity_scale)
            {
                return Err(SimulationError::InvalidTargetAllocationPolicy {
                    agent_id: policy.agent_id.clone(),
                    account_id: policy.account_id.clone(),
                });
            }
            for slot_index in 0..policy.purchase_slots_per_sleeve {
                let lot_id = format!(
                    "{}_buy_p{policy_index}_s{sleeve_index}_{slot_index}",
                    policy.cause_id_prefix
                );
                if !lots.insert(lot_id.clone()) {
                    return Err(SimulationError::DuplicateLot { lot_id });
                }
            }
        }
    }
    let mut tax_jurisdictions = BTreeSet::new();
    for profile in &fixture.scenario.tax_profiles {
        if profile.jurisdictions.is_empty() {
            return Err(SimulationError::EmptyTaxProfile {
                agent_id: profile.agent_id.clone(),
            });
        }
        if profile.prior_year_tax.0 < 0 || profile.section_121_exclusion.0 < 0 {
            return Err(SimulationError::InvalidAmount {
                kind: "tax profile prior-year tax",
                cause_id: profile.agent_id.clone(),
                amount: profile.prior_year_tax.0,
            });
        }
        validate_identifier("tax profile agent", &profile.agent_id)?;
        if !agents.contains(&profile.agent_id) {
            return Err(SimulationError::UnknownAccountReference {
                context: "tax profile".into(),
                agent_id: profile.agent_id.clone(),
                account_id: "checking".into(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&profile.agent_id, &profile.payment_account_id),
            "tax profile payment account",
        )?;
        validate_account(
            &accounts,
            &AccountRef::new(
                &profile.tax_authority_agent_id,
                &profile.tax_authority_account_id,
            ),
            "tax profile authority account",
        )?;
        for rules in &profile.jurisdictions {
            validate_identifier("tax jurisdiction", &rules.jurisdiction_id)?;
            validate_rules(rules)?;
            if !tax_jurisdictions.insert((profile.agent_id.clone(), rules.jurisdiction_id.clone()))
            {
                return Err(SimulationError::DuplicateTaxJurisdiction {
                    agent_id: profile.agent_id.clone(),
                    jurisdiction_id: rules.jurisdiction_id.clone(),
                });
            }
        }
    }
    let mut salt_profiles = BTreeSet::new();
    for policy in &fixture.scenario.federal_salt_deduction_policies {
        let Some(profile) = fixture
            .scenario
            .tax_profiles
            .iter()
            .find(|profile| profile.agent_id == policy.profile_id)
        else {
            return Err(SimulationError::InvalidSaltPolicy {
                profile_id: policy.profile_id.clone(),
            });
        };
        if !salt_profiles.insert(policy.profile_id.as_str())
            || !profile
                .jurisdictions
                .iter()
                .any(|rules| rules.jurisdiction_id == policy.federal_jurisdiction_id)
            || policy.cap_schedule.iter().any(|entry| entry.cap.0 < 0)
        {
            return Err(SimulationError::InvalidSaltPolicy {
                profile_id: policy.profile_id.clone(),
            });
        }
    }
    let mut locations = BTreeSet::new();
    for location in &fixture.scenario.locations {
        validate_identifier("location", &location.location_id)?;
        if !locations.insert(location.location_id.clone()) {
            return Err(SimulationError::DuplicateLocation {
                location_id: location.location_id.clone(),
            });
        }
        if location.annual_property_tax_rate_ppb < 0 || location.annual_special_assessment.0 < 0 {
            return Err(SimulationError::InvalidPropertyTaxPolicy {
                property_id: location.location_id.clone(),
            });
        }
    }
    let mut properties = BTreeMap::new();
    let mut mortgages = BTreeSet::new();
    let mut mortgage_owners = BTreeMap::new();
    for purchase in &fixture.scenario.scheduled_property_purchases {
        validate_identifier("property purchase", &purchase.cause_id)?;
        validate_identifier("property", &purchase.property_id)?;
        validate_event_month(
            "property purchase",
            &purchase.cause_id,
            purchase.month,
            fixture.scenario.horizon_months,
        )?;
        if !locations.contains(&purchase.location_id) {
            return Err(SimulationError::UnknownLocation {
                cause_id: purchase.cause_id.clone(),
                location_id: purchase.location_id.clone(),
            });
        }
        if properties
            .insert(purchase.property_id.clone(), purchase)
            .is_some()
        {
            return Err(SimulationError::DuplicateProperty {
                property_id: purchase.property_id.clone(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&purchase.buyer_agent_id, &purchase.buyer_account_id),
            &purchase.cause_id,
        )?;
        validate_account(
            &accounts,
            &AccountRef::new(&purchase.seller_agent_id, &purchase.seller_account_id),
            &purchase.cause_id,
        )?;
        let principal = purchase
            .mortgage
            .as_ref()
            .map_or(Money(0), |mortgage| mortgage.principal);
        if purchase.purchase_price.0 <= 0
            || purchase.down_payment.0 < 0
            || purchase.buyer_closing_cost.0 < 0
            || !(0..=RATE_SCALE_PPB).contains(&purchase.rented_fraction_ppb)
            || !(0..=RATE_SCALE_PPB).contains(&purchase.land_value_fraction_ppb)
            || purchase.down_payment.checked_add(principal)? != purchase.purchase_price
        {
            return Err(SimulationError::InvalidPropertyTerms {
                cause_id: purchase.cause_id.clone(),
            });
        }
        if let Some(mortgage) = &purchase.mortgage {
            validate_identifier("mortgage", &mortgage.liability_id)?;
            if !mortgages.insert(mortgage.liability_id.clone()) {
                return Err(SimulationError::DuplicateMortgage {
                    liability_id: mortgage.liability_id.clone(),
                });
            }
            mortgage_owners.insert(
                mortgage.liability_id.clone(),
                purchase.buyer_agent_id.clone(),
            );
            if mortgage.principal.0 <= 0
                || mortgage.annual_interest_rate_ppb < 0
                || mortgage.annual_interest_rate_ppb > RATE_SCALE_PPB
                || mortgage.term_months == 0
            {
                return Err(SimulationError::InvalidMortgageTerms {
                    liability_id: mortgage.liability_id.clone(),
                });
            }
            validate_account(
                &accounts,
                &AccountRef::new(&mortgage.lender_agent_id, &mortgage.lender_account_id),
                &mortgage.liability_id,
            )?;
            mortgage_monthly_payment(
                mortgage.principal,
                mortgage.annual_interest_rate_ppb,
                mortgage.term_months,
            )?;
        }
    }
    let mut mortgage_interest_policies = BTreeSet::new();
    for policy in &fixture.scenario.mortgage_interest_deduction_policies {
        let Some(owner_agent_id) = mortgage_owners.get(&policy.liability_id) else {
            return Err(SimulationError::UnknownMortgageInterestPolicy {
                liability_id: policy.liability_id.clone(),
            });
        };
        if owner_agent_id != &policy.owner_agent_id {
            return Err(SimulationError::InvalidMortgageInterestPolicy {
                liability_id: policy.liability_id.clone(),
            });
        }
        if !matches!(policy.debt_class.as_str(), "acquisition" | "home_equity")
            || policy
                .per_jurisdiction_principal_cap
                .values()
                .any(|cap| cap.0 < 0)
        {
            return Err(SimulationError::InvalidMortgageInterestPolicy {
                liability_id: policy.liability_id.clone(),
            });
        }
        for jurisdiction_id in policy.per_jurisdiction_principal_cap.keys() {
            validate_identifier("mortgage-interest jurisdiction", jurisdiction_id)?;
        }
        if !mortgage_interest_policies.insert(policy.liability_id.clone()) {
            return Err(SimulationError::InvalidMortgageInterestPolicy {
                liability_id: policy.liability_id.clone(),
            });
        }
    }
    let mut property_sales = BTreeSet::new();
    for sale in &fixture.scenario.property_sales {
        validate_identifier("property sale", &sale.property_id)?;
        validate_event_month(
            "property sale",
            &sale.property_id,
            sale.month,
            fixture.scenario.horizon_months,
        )?;
        let Some(purchase) = properties.get(&sale.property_id) else {
            return Err(SimulationError::UnknownPropertySale {
                property_id: sale.property_id.clone(),
            });
        };
        if !property_sales.insert(sale.property_id.clone()) {
            return Err(SimulationError::DuplicatePropertySale {
                property_id: sale.property_id.clone(),
            });
        }
        if sale.month <= purchase.month || sale.closing_cost_bps > 10_000 {
            return Err(SimulationError::InvalidPropertySale {
                property_id: sale.property_id.clone(),
            });
        }
        let series_id = format!("home_value:{}", purchase.location_id);
        if !series_ids.contains(&series_id) {
            return Err(SimulationError::MissingSeries { series_id });
        }
    }
    let sale_month_by_property: BTreeMap<_, _> = fixture
        .scenario
        .property_sales
        .iter()
        .map(|sale| (sale.property_id.as_str(), sale.month))
        .collect();
    let mut primary_initial_agents = BTreeSet::new();
    for assignment in &fixture.scenario.initial_primary_residences {
        if !primary_initial_agents.insert(assignment.agent_id.as_str()) {
            return Err(SimulationError::DuplicatePrimaryResidence {
                agent_id: assignment.agent_id.clone(),
                month: 0,
            });
        }
        validate_primary_residence_assignment(
            &agents,
            &properties,
            &sale_month_by_property,
            &assignment.agent_id,
            &assignment.property_id,
            0,
        )?;
    }
    let mut primary_event_keys = BTreeSet::new();
    for event in &fixture.scenario.primary_residence_events {
        validate_event_month(
            "primary residence",
            &event.agent_id,
            event.month,
            fixture.scenario.horizon_months,
        )?;
        if !primary_event_keys.insert((event.agent_id.as_str(), event.month)) {
            return Err(SimulationError::DuplicatePrimaryResidence {
                agent_id: event.agent_id.clone(),
                month: event.month,
            });
        }
        if let Some(property_id) = event.property_id.as_deref() {
            validate_primary_residence_assignment(
                &agents,
                &properties,
                &sale_month_by_property,
                &event.agent_id,
                property_id,
                event.month,
            )?;
        } else if !agents.contains(&event.agent_id) {
            return Err(SimulationError::InvalidPrimaryResidence {
                agent_id: event.agent_id.clone(),
                property_id: String::new(),
                month: event.month,
            });
        }
    }
    for event in &fixture.scenario.property_rented_fraction_events {
        validate_property_lifecycle_event(
            &properties,
            &fixture.scenario.property_sales,
            &event.property_id,
            event.month,
            fixture.scenario.horizon_months,
        )?;
        if !(0..=RATE_SCALE_PPB).contains(&event.rented_fraction_ppb) {
            return Err(SimulationError::InvalidPropertyLifecycle {
                property_id: event.property_id.clone(),
            });
        }
    }
    for event in &fixture.scenario.capital_improvement_events {
        validate_property_lifecycle_event(
            &properties,
            &fixture.scenario.property_sales,
            &event.property_id,
            event.month,
            fixture.scenario.horizon_months,
        )?;
        if event.amount.0 <= 0 {
            return Err(SimulationError::InvalidPropertyLifecycle {
                property_id: event.property_id.clone(),
            });
        }
    }
    let mut property_tax_months = BTreeSet::new();
    for policy in &fixture.scenario.property_tax_policies {
        let Some(purchase) = properties.get(&policy.property_id) else {
            return Err(SimulationError::UnknownPropertyTaxProperty {
                property_id: policy.property_id.clone(),
            });
        };
        if purchase.buyer_agent_id != policy.owner_agent_id
            || policy.annual_tax_rate_ppb.is_some_and(|rate| rate < 0)
            || policy.start_month >= fixture.scenario.horizon_months
            || policy.end_month.is_some_and(|end| {
                end < policy.start_month || end >= fixture.scenario.horizon_months
            })
        {
            return Err(SimulationError::InvalidPropertyTaxPolicy {
                property_id: policy.property_id.clone(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&policy.owner_agent_id, &policy.from_account_id),
            "property tax payer",
        )?;
        validate_account(
            &accounts,
            &AccountRef::new(
                &policy.tax_authority_agent_id,
                &policy.tax_authority_account_id,
            ),
            "property tax authority",
        )?;
        let end = policy
            .end_month
            .unwrap_or(fixture.scenario.horizon_months - 1);
        for month in policy.start_month..=end {
            if !property_tax_months.insert((policy.property_id.clone(), month)) {
                return Err(SimulationError::InvalidPropertyTaxPolicy {
                    property_id: policy.property_id.clone(),
                });
            }
        }
    }
    for cashflow in &fixture.scenario.scheduled_property_cashflows {
        validate_identifier("scheduled property cashflow", &cashflow.cause_id)?;
        validate_event_month(
            "scheduled property cashflow",
            &cashflow.cause_id,
            cashflow.month,
            fixture.scenario.horizon_months,
        )?;
        validate_amount_spec(
            fixture,
            "scheduled property cashflow",
            &cashflow.cause_id,
            &cashflow.amount,
            std::iter::once(cashflow.month),
        )?;
        validate_income_category(cashflow.income_category.as_deref())?;
        validate_income_category(cashflow.deduction_category.as_deref())?;
        validate_account(&accounts, &cashflow.from, &cashflow.cause_id)?;
        validate_account(&accounts, &cashflow.to, &cashflow.cause_id)?;
        validate_property_reference(
            &properties,
            "property cashflow",
            &cashflow.cause_id,
            &cashflow.property_id,
        )?;
    }
    for cashflow in &fixture.scenario.recurring_property_cashflows {
        validate_identifier("recurring property cashflow", &cashflow.cause_id)?;
        validate_event_month(
            "recurring property cashflow",
            &cashflow.cause_id,
            cashflow.start_month,
            fixture.scenario.horizon_months,
        )?;
        if let Some(end_month) = cashflow.end_month
            && end_month < cashflow.start_month
        {
            return Err(SimulationError::InvalidRecurringRange {
                kind: "recurring property cashflow",
                cause_id: cashflow.cause_id.clone(),
                start_month: cashflow.start_month,
                end_month,
            });
        }
        validate_amount_spec(
            fixture,
            "recurring property cashflow",
            &cashflow.cause_id,
            &cashflow.amount,
            cashflow.start_month
                ..=cashflow
                    .end_month
                    .unwrap_or(fixture.scenario.horizon_months - 1)
                    .min(fixture.scenario.horizon_months - 1),
        )?;
        validate_income_category(cashflow.income_category.as_deref())?;
        validate_income_category(cashflow.deduction_category.as_deref())?;
        validate_account(&accounts, &cashflow.from, &cashflow.cause_id)?;
        validate_account(&accounts, &cashflow.to, &cashflow.cause_id)?;
        validate_property_reference(
            &properties,
            "property cashflow",
            &cashflow.cause_id,
            &cashflow.property_id,
        )?;
    }
    for obligation in &fixture.scenario.obligations {
        if let Some(property_id) = obligation.property_id.as_deref() {
            validate_property_reference(
                &properties,
                "obligation",
                &obligation.obligation_id,
                property_id,
            )?;
        }
    }
    for obligation in &fixture.scenario.recurring_obligations {
        if let Some(property_id) = obligation.property_id.as_deref() {
            validate_property_reference(
                &properties,
                "recurring obligation",
                &obligation.obligation_id,
                property_id,
            )?;
        }
    }
    Ok(())
}

fn validate_property_reference(
    properties: &BTreeMap<String, &crate::fixture::ScheduledPropertyPurchaseSpec>,
    kind: &'static str,
    cause_id: &str,
    property_id: &str,
) -> Result<(), SimulationError> {
    if properties.contains_key(property_id) {
        return Ok(());
    }
    Err(SimulationError::UnknownPropertyReference {
        kind,
        cause_id: cause_id.into(),
        property_id: property_id.into(),
    })
}

fn validate_event_month(
    kind: &'static str,
    cause_id: &str,
    month: u32,
    horizon: u32,
) -> Result<(), SimulationError> {
    if month >= horizon {
        return Err(SimulationError::EventOutsideHorizon {
            kind,
            cause_id: cause_id.into(),
            month,
            horizon,
        });
    }
    Ok(())
}

fn validate_property_lifecycle_event(
    properties: &BTreeMap<String, &crate::fixture::ScheduledPropertyPurchaseSpec>,
    sales: &[PropertySaleSpec],
    property_id: &str,
    month: u32,
    horizon: u32,
) -> Result<(), SimulationError> {
    validate_identifier("property lifecycle", property_id)?;
    validate_event_month("property lifecycle", property_id, month, horizon)?;
    let Some(purchase) = properties.get(property_id) else {
        return Err(SimulationError::UnknownPropertyLifecycle {
            property_id: property_id.into(),
        });
    };
    if month <= purchase.month {
        return Err(SimulationError::InvalidPropertyLifecycle {
            property_id: property_id.into(),
        });
    }
    if sales
        .iter()
        .any(|sale| sale.property_id == property_id && month >= sale.month)
    {
        return Err(SimulationError::PropertyLifecycleAfterSale {
            property_id: property_id.into(),
        });
    }
    Ok(())
}

fn validate_primary_residence_assignment(
    agents: &BTreeSet<String>,
    properties: &BTreeMap<String, &crate::fixture::ScheduledPropertyPurchaseSpec>,
    sale_month_by_property: &BTreeMap<&str, u32>,
    agent_id: &str,
    property_id: &str,
    month: u32,
) -> Result<(), SimulationError> {
    let valid = agents.contains(agent_id)
        && properties.get(property_id).is_some_and(|purchase| {
            purchase.buyer_agent_id == agent_id
                && month >= purchase.month
                && sale_month_by_property
                    .get(property_id)
                    .is_none_or(|sale_month| month <= *sale_month)
        });
    if !valid {
        return Err(SimulationError::InvalidPrimaryResidence {
            agent_id: agent_id.into(),
            property_id: property_id.into(),
            month,
        });
    }
    Ok(())
}

fn validate_identifier(kind: &'static str, value: &str) -> Result<(), SimulationError> {
    if value.trim().is_empty() {
        return Err(SimulationError::EmptyIdentifier { kind });
    }
    Ok(())
}

fn validate_private_equity_channels(
    fixture: &Fixture,
    issuer_id: &str,
) -> Result<(), SimulationError> {
    let channel = |name: &str| -> Result<&SeriesSpec, SimulationError> {
        let series_id = private_equity_series_id(name, issuer_id);
        fixture
            .series
            .iter()
            .find(|series| series.series_id == series_id)
            .ok_or_else(|| SimulationError::MissingPrivateEquitySeries {
                issuer_id: issuer_id.to_owned(),
                series_id,
            })
    };
    let mark = channel("mark")?;
    let regime = channel("regime")?;
    let event_kind = channel("event_kind")?;
    let opportunity = channel("sale_opportunity")?;
    let capacity = channel("sale_capacity")?;
    let eligible = channel("eligible")?;
    let forced_sale = channel("forced_sale")?;
    let blocked = channel("liquidity_blocked")?;
    let recovery = channel("forced_recovery")?;
    let valuation = channel("company_valuation")?;
    validate_private_equity_series_range(issuer_id, "mark", mark, 0, i64::MAX)?;
    validate_private_equity_series_range(issuer_id, "regime", regime, 1, 4)?;
    validate_private_equity_series_range(issuer_id, "event_kind", event_kind, 0, 7)?;
    validate_private_equity_series_range(issuer_id, "sale_opportunity", opportunity, 0, 1)?;
    validate_private_equity_series_range(issuer_id, "sale_capacity", capacity, 0, RATE_SCALE_PPB)?;
    validate_private_equity_series_range(issuer_id, "eligible", eligible, 0, RATE_SCALE_PPB)?;
    validate_private_equity_series_range(issuer_id, "forced_sale", forced_sale, 0, RATE_SCALE_PPB)?;
    validate_private_equity_series_range(issuer_id, "liquidity_blocked", blocked, 0, 1)?;
    validate_private_equity_series_range(issuer_id, "forced_recovery", recovery, 0, i64::MAX)?;
    validate_private_equity_series_range(issuer_id, "company_valuation", valuation, 0, i64::MAX)?;
    for (index, (event, active)) in event_kind
        .values
        .iter()
        .zip(&opportunity.values)
        .enumerate()
    {
        if (*event == 1) != (*active == 1) {
            return Err(SimulationError::InvalidPrivateEquityChannel {
                issuer_id: issuer_id.to_owned(),
                channel: "event/opportunity consistency",
                index,
                value: *event,
            });
        }
    }
    Ok(())
}

fn validate_private_equity_series_range(
    issuer_id: &str,
    channel: &'static str,
    series: &SeriesSpec,
    minimum: i64,
    maximum: i64,
) -> Result<(), SimulationError> {
    if let Some((index, value)) = series
        .values
        .iter()
        .copied()
        .enumerate()
        .find(|(_, value)| !(minimum..=maximum).contains(value))
    {
        return Err(SimulationError::InvalidPrivateEquityChannel {
            issuer_id: issuer_id.to_owned(),
            channel,
            index,
            value,
        });
    }
    Ok(())
}

fn is_positive_decimal(value: &str) -> bool {
    let mut saw_digit = false;
    let mut saw_nonzero = false;
    let mut saw_dot = false;
    let bytes = value.as_bytes();
    if bytes.is_empty() || bytes.first() == Some(&b'.') || bytes.last() == Some(&b'.') {
        return false;
    }
    for byte in bytes {
        match byte {
            b'0'..=b'9' => {
                saw_digit = true;
                saw_nonzero |= *byte != b'0';
            }
            b'.' if !saw_dot => saw_dot = true,
            _ => return false,
        }
    }
    saw_digit && saw_nonzero
}

fn validate_amount_spec(
    fixture: &Fixture,
    kind: &'static str,
    cause_id: &str,
    amount: &AmountSpec,
    months: impl IntoIterator<Item = u32>,
) -> Result<(), SimulationError> {
    let AmountSpec::SeriesIndexed(amount) = amount else {
        return Ok(());
    };
    if amount.adjustment_period_months == 0 {
        return Err(SimulationError::InvalidSeriesIndexedAmount {
            kind,
            cause_id: cause_id.into(),
        });
    }
    let valid_rent_series = amount
        .series_id
        .strip_prefix("rent:")
        .is_some_and(|location_id| !location_id.is_empty());
    if amount.series_id != "inflation" && !valid_rent_series {
        return Err(SimulationError::UnsupportedAmountSeries {
            kind,
            cause_id: cause_id.into(),
            series_id: amount.series_id.clone(),
        });
    }
    let months = months.into_iter().collect::<Vec<_>>();
    if let Some(month) = months
        .iter()
        .copied()
        .find(|month| *month < amount.base_month_index)
    {
        return Err(SimulationError::SeriesAmountBeforeBase {
            kind,
            cause_id: cause_id.into(),
            month,
            base_month: amount.base_month_index,
        });
    }
    let series = fixture
        .series
        .iter()
        .find(|series| series.series_id == amount.series_id)
        .ok_or_else(|| SimulationError::MissingSeries {
            series_id: amount.series_id.clone(),
        })?;
    let mut required_months = BTreeSet::from([amount.base_month_index]);
    for month in months {
        let elapsed = month - amount.base_month_index;
        let reset_month = amount.base_month_index
            + (elapsed / amount.adjustment_period_months) * amount.adjustment_period_months;
        required_months.insert(reset_month);
    }
    for rollout in 0..fixture.rollout_count {
        for reset_month in &required_months {
            let value = series.value(rollout, *reset_month).ok_or_else(|| {
                SimulationError::MissingSeriesValue {
                    series_id: amount.series_id.clone(),
                    rollout,
                    snapshot: *reset_month,
                }
            })?;
            validate_amount_index_level(cause_id, &amount.series_id, rollout, *reset_month, value)?;
        }
    }
    Ok(())
}

fn validate_amount_index_level(
    cause_id: &str,
    series_id: &str,
    rollout: u32,
    month: u32,
    value: i64,
) -> Result<(), SimulationError> {
    if value <= 0 {
        return Err(SimulationError::NonPositiveSeriesAmountLevel {
            cause_id: cause_id.into(),
            series_id: series_id.into(),
            rollout,
            month,
            value,
        });
    }
    let reconstructed =
        ((value as f64 / INDEX_LEVEL_SCALE as f64) * INDEX_LEVEL_SCALE as f64).round() as i64;
    if value > MAX_EXACT_F64_INTEGER || reconstructed != value {
        return Err(SimulationError::InexactSeriesAmountLevel {
            cause_id: cause_id.into(),
            series_id: series_id.into(),
            rollout,
            month,
            value,
        });
    }
    Ok(())
}

fn validate_income_category(category: Option<&str>) -> Result<(), SimulationError> {
    if let Some(category) = category
        && category != "ordinary"
    {
        return Err(SimulationError::UnsupportedIncomeCategory {
            category: category.into(),
        });
    }
    Ok(())
}

fn validate_account(
    accounts: &BTreeSet<AccountRef>,
    account: &AccountRef,
    context: &str,
) -> Result<(), SimulationError> {
    if !accounts.contains(account) {
        return Err(SimulationError::UnknownAccountReference {
            context: context.into(),
            agent_id: account.agent_id.clone(),
            account_id: account.account_id.clone(),
        });
    }
    Ok(())
}
