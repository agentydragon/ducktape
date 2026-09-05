//! Output capture: what each capture mode retains, and the serializers that turn live
//! rollout state into the snapshot records.

use super::*;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum CaptureMode {
    Summary,
    Dense,
    Forensic,
}

impl CaptureMode {
    pub(super) fn captures_output(self) -> bool {
        self != Self::Summary
    }

    fn captures_journal(self) -> bool {
        self == Self::Forensic
    }
}

#[derive(Debug)]
pub(super) struct Recorder {
    pub(super) capture_mode: CaptureMode,
    pub(super) months: Vec<MonthOutput>,
    pub(super) journal: Vec<JournalEntry>,
    pub(super) transfers: Vec<TransferOutcome>,
    pub(super) dispositions: Vec<LotDisposition>,
    pub(super) private_equity_events: Vec<PrivateEquityProtocolOutcome>,
    pub(super) private_equity_opportunities: Vec<PrivateEquityOpportunityOutcome>,
    pub(super) obligations: Vec<ObligationOutcome>,
    pub(super) rollout_failures: Vec<RolloutFailureOutcome>,
    pub(super) tax_accruals: Vec<TaxAccrual>,
    pub(super) tax_payments: Vec<TaxPaymentOutcome>,
    pub(super) tax_settlements: Vec<TaxSettlementOutcome>,
    pub(super) bond_cashflows: Vec<BondCashflowOutcome>,
    pub(super) distributions: Vec<DistributionOutcome>,
    pub(super) property_purchases: Vec<PropertyPurchaseOutcome>,
    pub(super) primary_residence_events: Vec<PrimaryResidenceOutcome>,
    pub(super) property_rented_fraction_events: Vec<PropertyRentedFractionOutcome>,
    pub(super) capital_improvements: Vec<CapitalImprovementOutcome>,
    pub(super) property_sales: Vec<PropertySaleOutcome>,
    pub(super) mortgage_originations: Vec<MortgageOriginationOutcome>,
    pub(super) mortgage_payments: Vec<MortgagePaymentOutcome>,
    pub(super) journal_entry_count: u64,
    pub(super) disposition_count: u64,
    pub(super) private_equity_event_count: u64,
    pub(super) private_equity_opportunity_count: u64,
    pub(super) tax_accrual_count: u64,
    pub(super) tax_payment_count: u64,
    pub(super) tax_settlement_count: u64,
    pub(super) bond_cashflow_count: u64,
    pub(super) distribution_count: u64,
    pub(super) property_purchase_count: u64,
    pub(super) primary_residence_event_count: u64,
    pub(super) property_rented_fraction_event_count: u64,
    pub(super) capital_improvement_count: u64,
    pub(super) property_sale_count: u64,
    pub(super) mortgage_payment_count: u64,
}

impl Recorder {
    pub(super) fn new(capture_mode: CaptureMode) -> Self {
        Self {
            capture_mode,
            months: Vec::new(),
            journal: Vec::new(),
            transfers: Vec::new(),
            dispositions: Vec::new(),
            private_equity_events: Vec::new(),
            private_equity_opportunities: Vec::new(),
            obligations: Vec::new(),
            rollout_failures: Vec::new(),
            tax_accruals: Vec::new(),
            tax_payments: Vec::new(),
            tax_settlements: Vec::new(),
            bond_cashflows: Vec::new(),
            distributions: Vec::new(),
            property_purchases: Vec::new(),
            primary_residence_events: Vec::new(),
            property_rented_fraction_events: Vec::new(),
            capital_improvements: Vec::new(),
            property_sales: Vec::new(),
            mortgage_originations: Vec::new(),
            mortgage_payments: Vec::new(),
            journal_entry_count: 0,
            disposition_count: 0,
            private_equity_event_count: 0,
            private_equity_opportunity_count: 0,
            tax_accrual_count: 0,
            tax_payment_count: 0,
            tax_settlement_count: 0,
            bond_cashflow_count: 0,
            distribution_count: 0,
            property_purchase_count: 0,
            primary_residence_event_count: 0,
            property_rented_fraction_event_count: 0,
            capital_improvement_count: 0,
            property_sale_count: 0,
            mortgage_payment_count: 0,
        }
    }

    pub(super) fn apply_entry(
        &mut self,
        ledger: &mut Ledger,
        entry: JournalEntry,
    ) -> Result<(), SimulationError> {
        ledger.apply(&entry)?;
        self.journal_entry_count =
            self.journal_entry_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "journal entry count",
                })?;
        if self.capture_mode.captures_journal() {
            self.journal.push(entry);
        }
        Ok(())
    }

    pub(super) fn record_disposition(
        &mut self,
        disposition: LotDisposition,
    ) -> Result<(), SimulationError> {
        self.disposition_count =
            self.disposition_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "disposition count",
                })?;
        if self.capture_mode.captures_output() {
            self.dispositions.push(disposition);
        }
        Ok(())
    }

    pub(super) fn record_private_equity_event(
        &mut self,
        event: PrivateEquityProtocolOutcome,
    ) -> Result<(), SimulationError> {
        self.private_equity_event_count =
            self.private_equity_event_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "private-equity event count",
                })?;
        if self.capture_mode.captures_output() {
            self.private_equity_events.push(event);
        }
        Ok(())
    }

    pub(super) fn record_private_equity_opportunity(
        &mut self,
        opportunity: PrivateEquityOpportunityOutcome,
    ) -> Result<(), SimulationError> {
        self.private_equity_opportunity_count = self
            .private_equity_opportunity_count
            .checked_add(1)
            .ok_or(ArithmeticError::Overflow {
                operation: "private-equity opportunity count",
            })?;
        if self.capture_mode.captures_output() {
            self.private_equity_opportunities.push(opportunity);
        }
        Ok(())
    }

    pub(super) fn record_transfer(&mut self, transfer: TransferOutcome) {
        if self.capture_mode.captures_output() {
            self.transfers.push(transfer);
        }
    }

    pub(super) fn record_obligation(&mut self, obligation: ObligationOutcome) {
        if self.capture_mode.captures_output() {
            self.obligations.push(obligation);
        }
    }

    pub(super) fn record_rollout_failure(&mut self, failure: RolloutFailureOutcome) {
        if self.capture_mode.captures_output() {
            self.rollout_failures.push(failure);
        }
    }

    pub(super) fn record_tax_accrual(
        &mut self,
        accrual: TaxAccrual,
    ) -> Result<(), SimulationError> {
        self.tax_accrual_count =
            self.tax_accrual_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "tax accrual count",
                })?;
        if self.capture_mode.captures_output() {
            self.tax_accruals.push(accrual);
        }
        Ok(())
    }

    pub(super) fn record_tax_payment(
        &mut self,
        payment: TaxPaymentOutcome,
    ) -> Result<(), SimulationError> {
        self.tax_payment_count =
            self.tax_payment_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "tax payment count",
                })?;
        if self.capture_mode.captures_output() {
            self.tax_payments.push(payment);
        }
        Ok(())
    }

    pub(super) fn record_tax_settlement(
        &mut self,
        settlement: TaxSettlementOutcome,
    ) -> Result<(), SimulationError> {
        self.tax_settlement_count =
            self.tax_settlement_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "tax settlement count",
                })?;
        if self.capture_mode.captures_output() {
            self.tax_settlements.push(settlement);
        }
        Ok(())
    }

    pub(super) fn record_distribution(
        &mut self,
        distribution: DistributionOutcome,
    ) -> Result<(), SimulationError> {
        self.distribution_count =
            self.distribution_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "distribution count",
                })?;
        if self.capture_mode.captures_output() {
            self.distributions.push(distribution);
        }
        Ok(())
    }

    pub(super) fn record_bond_cashflow(
        &mut self,
        cashflow: BondCashflowOutcome,
    ) -> Result<(), SimulationError> {
        self.bond_cashflow_count =
            self.bond_cashflow_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "bond cashflow count",
                })?;
        if self.capture_mode.captures_output() {
            self.bond_cashflows.push(cashflow);
        }
        Ok(())
    }

    pub(super) fn record_property_purchase(
        &mut self,
        purchase: PropertyPurchaseOutcome,
        origination: Option<MortgageOriginationOutcome>,
    ) -> Result<(), SimulationError> {
        self.property_purchase_count =
            self.property_purchase_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "property purchase count",
                })?;
        if self.capture_mode.captures_output() {
            self.property_purchases.push(purchase);
            if let Some(origination) = origination {
                self.mortgage_originations.push(origination);
            }
        }
        Ok(())
    }

    pub(super) fn record_property_sale(
        &mut self,
        sale: PropertySaleOutcome,
    ) -> Result<(), SimulationError> {
        self.property_sale_count =
            self.property_sale_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "property sale count",
                })?;
        if self.capture_mode.captures_output() {
            self.property_sales.push(sale);
        }
        Ok(())
    }

    pub(super) fn record_primary_residence(
        &mut self,
        event: PrimaryResidenceOutcome,
    ) -> Result<(), SimulationError> {
        self.primary_residence_event_count = self
            .primary_residence_event_count
            .checked_add(1)
            .ok_or(ArithmeticError::Overflow {
                operation: "primary-residence event count",
            })?;
        if self.capture_mode.captures_output() {
            self.primary_residence_events.push(event);
        }
        Ok(())
    }

    pub(super) fn record_property_rented_fraction(
        &mut self,
        event: PropertyRentedFractionOutcome,
    ) -> Result<(), SimulationError> {
        self.property_rented_fraction_event_count = self
            .property_rented_fraction_event_count
            .checked_add(1)
            .ok_or(ArithmeticError::Overflow {
                operation: "property rented-fraction event count",
            })?;
        if self.capture_mode.captures_output() {
            self.property_rented_fraction_events.push(event);
        }
        Ok(())
    }

    pub(super) fn record_capital_improvement(
        &mut self,
        event: CapitalImprovementOutcome,
    ) -> Result<(), SimulationError> {
        self.capital_improvement_count =
            self.capital_improvement_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "capital improvement count",
                })?;
        if self.capture_mode.captures_output() {
            self.capital_improvements.push(event);
        }
        Ok(())
    }

    pub(super) fn record_mortgage_payment(
        &mut self,
        payment: MortgagePaymentOutcome,
    ) -> Result<(), SimulationError> {
        self.mortgage_payment_count =
            self.mortgage_payment_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "mortgage payment count",
                })?;
        if self.capture_mode.captures_output() {
            self.mortgage_payments.push(payment);
        }
        Ok(())
    }

    pub(super) fn record_month(&mut self, month: MonthOutput) {
        if self.capture_mode.captures_output() {
            self.months.push(month);
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn month_output(
    fixture: &Fixture,
    rollout_id: u32,
    month: u32,
    ledger: &Ledger,
    lots: &[LotState],
    properties: &[PropertyState],
    mortgages: &[MortgageState],
    tax_liabilities: &[TaxLiabilityState],
    tax_facts: &BTreeMap<(String, String), TaxFacts>,
    tlh_cumulative_harvest: &[Money],
    failed: bool,
) -> Result<MonthOutput, SimulationError> {
    Ok(MonthOutput {
        month,
        balances: account_balances(ledger, failed),
        lots: security_lot_states(lots, failed),
        bonds: bond_states(fixture, rollout_id, month, failed)?,
        properties: property_states(properties, failed),
        mortgages: mortgage_states(mortgages, failed),
        tax_liabilities: tax_liability_states(tax_liabilities, failed),
        capital_gains: capital_gain_states(fixture, tax_facts, failed),
        tlh_cumulative_harvest: if failed {
            vec![Money(0); tlh_cumulative_harvest.len()]
        } else {
            tlh_cumulative_harvest.to_vec()
        },
        failed,
    })
}

fn capital_gain_states(
    fixture: &Fixture,
    tax_facts: &BTreeMap<(String, String), TaxFacts>,
    failed: bool,
) -> Vec<CapitalGainState> {
    fixture
        .scenario
        .tax_profiles
        .iter()
        .map(|profile| {
            let facts = tax_facts
                .get(&(
                    profile.agent_id.clone(),
                    profile.jurisdictions[0].jurisdiction_id.clone(),
                ))
                .expect("validated tax profile has representative facts");
            CapitalGainState {
                agent_id: profile.agent_id.clone(),
                short_term_gain: if failed {
                    Money(0)
                } else {
                    facts.short_term_gain
                },
                long_term_gain: if failed {
                    Money(0)
                } else {
                    facts.long_term_gain
                },
            }
        })
        .collect()
}

fn security_lot_states(lots: &[LotState], failed: bool) -> Vec<SecurityLotState> {
    lots.iter()
        .map(|lot| SecurityLotState {
            lot_id: lot.spec.lot_id.clone(),
            agent_id: lot.spec.agent_id.clone(),
            account_id: lot.spec.account_id.clone(),
            asset_id: canonical_lot_asset_id(&lot.spec.asset_id),
            purchase_month: lot.spec.purchase_month,
            quantity_scale: lot.spec.quantity_scale,
            units_remaining: if failed {
                Quantity(0)
            } else {
                lot.units_remaining
            },
            basis_remaining: if failed {
                Money(0)
            } else {
                lot.basis_remaining
            },
            cost_basis_per_unit: lot.basis_per_unit,
        })
        .collect()
}

pub(super) fn tax_liability_states(
    tax_liabilities: &[TaxLiabilityState],
    failed: bool,
) -> Vec<TaxLiabilityState> {
    tax_liabilities
        .iter()
        .cloned()
        .map(|mut liability| {
            if failed {
                liability.amount_owed = Money(0);
            }
            liability
        })
        .collect()
}

pub(super) fn property_states(properties: &[PropertyState], failed: bool) -> Vec<PropertyState> {
    properties
        .iter()
        .cloned()
        .map(|mut property| {
            if failed {
                property.adjusted_basis = Money(0);
                property.contribution_used = Money(0);
                property.equity_ledger = Money(0);
            }
            property
        })
        .collect()
}

pub(super) fn mortgage_states(mortgages: &[MortgageState], failed: bool) -> Vec<MortgageState> {
    mortgages
        .iter()
        .cloned()
        .map(|mut mortgage| {
            if failed {
                mortgage.monthly_payment = Money(0);
                mortgage.principal = Money(0);
                mortgage.interest_paid_ytd = Money(0);
            }
            mortgage
        })
        .collect()
}

pub(super) fn account_balances(ledger: &Ledger, failed: bool) -> Vec<AccountBalance> {
    ledger
        .balances()
        .iter()
        .map(|(account, balance)| AccountBalance {
            account: account.clone(),
            balance: if failed { Money(0) } else { *balance },
        })
        .collect()
}
