use std::{
    env,
    fs::File,
    hint::black_box,
    io::{BufReader, Write},
    time::Instant,
};

use augur_rust_simulator::{
    Fixture, PopulationOutput, SimulationOutput, ValidatedFixture, simulate_dense_validated,
    simulate_summaries_validated,
};
use serde::Serialize;

#[derive(Clone, Copy, Debug)]
enum OutputMode {
    Compact,
    Dense,
}

impl OutputMode {
    fn parse(value: Option<&str>) -> Result<Self, String> {
        match value.unwrap_or("compact") {
            "compact" => Ok(Self::Compact),
            "dense" => Ok(Self::Dense),
            value => Err(format!(
                "output mode must be `compact` or `dense`; got {value:?}"
            )),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Compact => "compact",
            Self::Dense => "dense",
        }
    }
}

#[derive(Debug)]
enum BenchmarkOutput {
    Compact(PopulationOutput),
    Dense(SimulationOutput),
}

#[derive(Debug, Default)]
struct OutputCounts {
    state_snapshot_count: u64,
    retained_journal_entry_count: u64,
    retained_event_count: u64,
    canonical_event_row_count: u64,
    disposition_count: u64,
    private_equity_event_count: u64,
    private_equity_opportunity_count: u64,
    tax_accrual_count: u64,
    tax_payment_count: u64,
    tax_settlement_count: u64,
    bond_cashflow_count: u64,
    distribution_count: u64,
    property_purchase_count: u64,
    primary_residence_event_count: u64,
    property_rented_fraction_event_count: u64,
    capital_improvement_count: u64,
    property_sale_count: u64,
    mortgage_origination_count: u64,
    mortgage_payment_count: u64,
    failure_count: u64,
}

impl BenchmarkOutput {
    fn counts(&self) -> OutputCounts {
        match self {
            Self::Compact(output) => {
                let mut counts = OutputCounts::default();
                for rollout in &output.rollouts {
                    counts.disposition_count += rollout.disposition_count;
                    counts.private_equity_event_count += rollout.private_equity_event_count;
                    counts.private_equity_opportunity_count +=
                        rollout.private_equity_opportunity_count;
                    counts.tax_accrual_count += rollout.tax_accrual_count;
                    counts.tax_payment_count += rollout.tax_payment_count;
                    counts.tax_settlement_count += rollout.tax_settlement_count;
                    counts.bond_cashflow_count += rollout.bond_cashflow_count;
                    counts.distribution_count += rollout.distribution_count;
                    counts.property_purchase_count += rollout.property_purchase_count;
                    counts.primary_residence_event_count += rollout.primary_residence_event_count;
                    counts.property_rented_fraction_event_count +=
                        rollout.property_rented_fraction_event_count;
                    counts.capital_improvement_count += rollout.capital_improvement_count;
                    counts.property_sale_count += rollout.property_sale_count;
                    counts.mortgage_payment_count += rollout.mortgage_payment_count;
                    counts.failure_count += u64::from(rollout.failed_month.is_some());
                }
                counts
            }
            Self::Dense(output) => {
                let mut counts = OutputCounts::default();
                for rollout in &output.rollouts {
                    counts.state_snapshot_count += rollout.months.len() as u64;
                    counts.retained_journal_entry_count += rollout.journal.len() as u64;
                    counts.disposition_count += rollout.dispositions.len() as u64;
                    counts.private_equity_event_count += rollout.private_equity_events.len() as u64;
                    counts.private_equity_opportunity_count +=
                        rollout.private_equity_opportunities.len() as u64;
                    counts.tax_accrual_count += rollout.tax_accruals.len() as u64;
                    counts.tax_payment_count += rollout.tax_payments.len() as u64;
                    counts.tax_settlement_count += rollout.tax_settlements.len() as u64;
                    counts.bond_cashflow_count += rollout.bond_cashflows.len() as u64;
                    counts.distribution_count += rollout.distributions.len() as u64;
                    counts.property_purchase_count += rollout.property_purchases.len() as u64;
                    counts.primary_residence_event_count +=
                        rollout.primary_residence_events.len() as u64;
                    counts.property_rented_fraction_event_count +=
                        rollout.property_rented_fraction_events.len() as u64;
                    counts.capital_improvement_count += rollout.capital_improvements.len() as u64;
                    counts.property_sale_count += rollout.property_sales.len() as u64;
                    counts.mortgage_origination_count += rollout.mortgage_originations.len() as u64;
                    counts.mortgage_payment_count += rollout.mortgage_payments.len() as u64;
                    counts.failure_count += u64::from(rollout.failed_month.is_some());
                    counts.retained_event_count += rollout.transfers.len() as u64
                        + rollout.dispositions.len() as u64
                        + rollout.private_equity_events.len() as u64
                        + rollout.private_equity_opportunities.len() as u64
                        + rollout.obligations.len() as u64
                        + rollout.rollout_failures.len() as u64
                        + rollout.tax_accruals.len() as u64
                        + rollout.tax_payments.len() as u64
                        + rollout.tax_settlements.len() as u64
                        + rollout.bond_cashflows.len() as u64
                        + rollout.distributions.len() as u64
                        + rollout.property_purchases.len() as u64
                        + rollout.primary_residence_events.len() as u64
                        + rollout.property_rented_fraction_events.len() as u64
                        + rollout.capital_improvements.len() as u64
                        + rollout.property_sales.len() as u64
                        + rollout.mortgage_originations.len() as u64
                        + rollout.mortgage_payments.len() as u64;
                    counts.canonical_event_row_count += rollout.transfers.len() as u64
                        + rollout.dispositions.len() as u64
                        + rollout.private_equity_events.len() as u64
                        + rollout.private_equity_opportunities.len() as u64
                        + 2 * rollout.obligations.len() as u64
                        + rollout.rollout_failures.len() as u64
                        + 2 * rollout.tax_accruals.len() as u64
                        + rollout.tax_settlements.len() as u64
                        + rollout.property_purchases.len() as u64
                        + rollout.primary_residence_events.len() as u64
                        + rollout.property_rented_fraction_events.len() as u64
                        + rollout.capital_improvements.len() as u64
                        + rollout.property_sales.len() as u64
                        + rollout.mortgage_originations.len() as u64
                        + rollout.mortgage_payments.len() as u64;
                }
                counts
            }
        }
    }

    fn checksum(&self) -> Result<u64, serde_json::Error> {
        match self {
            Self::Compact(output) => checksum_serializable(output),
            Self::Dense(output) => checksum_serializable(output),
        }
    }
}

#[derive(Debug, Serialize)]
struct BenchmarkReport {
    output_mode: &'static str,
    rollout_count: u32,
    horizon_months: u32,
    repeats: usize,
    cold_wall_seconds: f64,
    wall_seconds: Vec<f64>,
    median_wall_seconds: f64,
    rollouts_per_second: f64,
    rollout_months_per_second: f64,
    state_snapshot_count: u64,
    retained_journal_entry_count: u64,
    retained_event_count: u64,
    canonical_event_row_count: u64,
    disposition_count: u64,
    private_equity_event_count: u64,
    private_equity_opportunity_count: u64,
    tax_accrual_count: u64,
    tax_payment_count: u64,
    tax_settlement_count: u64,
    bond_cashflow_count: u64,
    distribution_count: u64,
    property_purchase_count: u64,
    primary_residence_event_count: u64,
    property_rented_fraction_event_count: u64,
    capital_improvement_count: u64,
    property_sale_count: u64,
    mortgage_origination_count: u64,
    mortgage_payment_count: u64,
    failure_count: u64,
    checksum: u64,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args_os();
    let program = args.next().unwrap_or_default();
    let input = args.next().ok_or_else(|| usage(&program))?;
    let repeats = args
        .next()
        .map(|value| value.to_string_lossy().parse::<usize>())
        .transpose()?
        .unwrap_or(5);
    let output_mode_argument = args.next();
    let output_mode = OutputMode::parse(
        output_mode_argument
            .as_deref()
            .and_then(|value| value.to_str()),
    )?;
    if repeats == 0 || args.next().is_some() {
        return Err(usage(&program).into());
    }

    // Parsing and fixture validation are intentionally outside the timed region. The dense mode
    // retains monthly state and compatibility-event records but omits Rust's additional forensic
    // journal, for which Python/JAX has no corresponding output channel.
    let fixture: Fixture = serde_json::from_reader(BufReader::new(File::open(input)?))?;
    let rollout_count = fixture.rollout_count;
    let horizon_months = fixture.scenario.horizon_months;
    let fixture = ValidatedFixture::new(&fixture)?;
    let run_once = || -> Result<BenchmarkOutput, Box<dyn std::error::Error>> {
        Ok(match output_mode {
            OutputMode::Compact => BenchmarkOutput::Compact(simulate_summaries_validated(fixture)?),
            OutputMode::Dense => BenchmarkOutput::Dense(simulate_dense_validated(fixture)?),
        })
    };
    let cold_started = Instant::now();
    black_box(run_once()?);
    let cold_wall_seconds = cold_started.elapsed().as_secs_f64();

    let mut durations = Vec::with_capacity(repeats);
    let mut last_output = None;
    for _ in 0..repeats {
        drop(last_output.take());
        let started = Instant::now();
        let output = black_box(run_once()?);
        durations.push(started.elapsed().as_secs_f64());
        last_output = Some(output);
    }
    let output = last_output.expect("positive repeat count");
    let mut sorted = durations.clone();
    sorted.sort_by(f64::total_cmp);
    let median = sorted[sorted.len() / 2];
    let rollout_months = f64::from(rollout_count) * f64::from(horizon_months);
    let counts = output.counts();
    let report = BenchmarkReport {
        output_mode: output_mode.as_str(),
        rollout_count,
        horizon_months,
        repeats,
        cold_wall_seconds,
        wall_seconds: durations,
        median_wall_seconds: median,
        rollouts_per_second: f64::from(rollout_count) / median,
        rollout_months_per_second: rollout_months / median,
        state_snapshot_count: counts.state_snapshot_count,
        retained_journal_entry_count: counts.retained_journal_entry_count,
        retained_event_count: counts.retained_event_count,
        canonical_event_row_count: counts.canonical_event_row_count,
        disposition_count: counts.disposition_count,
        private_equity_event_count: counts.private_equity_event_count,
        private_equity_opportunity_count: counts.private_equity_opportunity_count,
        tax_accrual_count: counts.tax_accrual_count,
        tax_payment_count: counts.tax_payment_count,
        tax_settlement_count: counts.tax_settlement_count,
        bond_cashflow_count: counts.bond_cashflow_count,
        distribution_count: counts.distribution_count,
        property_purchase_count: counts.property_purchase_count,
        primary_residence_event_count: counts.primary_residence_event_count,
        property_rented_fraction_event_count: counts.property_rented_fraction_event_count,
        capital_improvement_count: counts.capital_improvement_count,
        property_sale_count: counts.property_sale_count,
        mortgage_origination_count: counts.mortgage_origination_count,
        mortgage_payment_count: counts.mortgage_payment_count,
        failure_count: counts.failure_count,
        checksum: output.checksum()?,
    };
    serde_json::to_writer(std::io::stdout().lock(), &report)?;
    println!();
    Ok(())
}

fn usage(program: &std::ffi::OsStr) -> String {
    format!(
        "usage: {} FIXTURE.json [REPEATS] [compact|dense]",
        program.to_string_lossy()
    )
}

struct HashWriter {
    hash: u64,
}

impl HashWriter {
    fn new() -> Self {
        Self {
            hash: 0xcbf2_9ce4_8422_2325,
        }
    }
}

impl Write for HashWriter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        for byte in bytes {
            self.hash ^= u64::from(*byte);
            self.hash = self.hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn checksum_serializable<T: Serialize>(value: &T) -> Result<u64, serde_json::Error> {
    let mut writer = HashWriter::new();
    serde_json::to_writer(&mut writer, value)?;
    Ok(writer.hash)
}
