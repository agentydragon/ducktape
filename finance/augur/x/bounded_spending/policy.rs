//! An experiment-owned annual rule: target a portfolio percentage, bounded by the
//! previous withdrawal's inflation-adjusted cut/raise limits. No engine rule registry.

use std::{
    env,
    fs::File,
    io::{BufReader, BufWriter},
    path::Path,
};

use augur_rust_simulator::{
    engine::{
        SimulationError,
        spending::{self, Observation, Spending},
    },
    execution::ExecutionInput,
    ledger::AccountRef,
    money::{Factor, Money},
};

fn annual_spending(
    rate_bps: u32,
    max_cut_bps: u32,
    max_raise_bps: u32,
) -> impl FnMut(Observation) -> Result<Money, SimulationError> {
    let mut previous: Option<(Money, i64)> = None;
    move |observation| {
        if observation.month % 12 != 0 {
            return Ok(Money(0));
        }
        let target = observation
            .cash
            .checked_add(observation.public_holdings)?
            .scaled_by(
                Factor::new(i64::from(rate_bps), 10_000),
                "portfolio spending target",
            )?;
        let current_cpi = observation.price_level.numerator();
        let request = match previous {
            None => target,
            Some((amount, prior_cpi)) => {
                let indexed =
                    amount.scaled_by(Factor::new(current_cpi, prior_cpi), "annual CPI reset")?;
                let lower = indexed.scaled_by(
                    Factor::new(10_000 - i64::from(max_cut_bps), 10_000),
                    "spending cut limit",
                )?;
                let upper = indexed.scaled_by(
                    Factor::new(10_000 + i64::from(max_raise_bps), 10_000),
                    "spending raise limit",
                )?;
                Money(target.0.clamp(lower.0, upper.0))
            }
        };
        previous = Some((request, current_cpi));
        Ok(request)
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args().skip(1).collect();
    let [input, output, rate, cut, raise, trace_rollouts @ ..] = args.as_slice() else {
        return Err(
            "usage: runner EXECUTION-INPUT.json SUMMARY.json RATE_BPS MAX_CUT_BPS MAX_RAISE_BPS [TRACE_ROLLOUT ...]"
                .into(),
        );
    };
    let rate: u32 = rate.parse()?;
    let cut: u32 = cut.parse()?;
    let raise: u32 = raise.parse()?;
    if rate == 0 || rate > 10_000 || cut > 10_000 || raise > 10_000 {
        return Err("rate must be in (0, 10000] bps; cut and raise in [0, 10000] bps".into());
    }
    let execution_input: ExecutionInput =
        serde_json::from_reader(BufReader::new(File::open(input)?))?;
    let component = Spending {
        from: AccountRef::new("retiree", "checking"),
        to: AccountRef::new("world", "checking"),
        cause_id: "annual_consumption".into(),
    };
    let output_run = spending::simulate_summary(&execution_input, &component, |_| {
        annual_spending(rate, cut, raise)
    })?;
    serde_json::to_writer(BufWriter::new(File::create(output)?), &output_run)?;
    for rollout in trace_rollouts {
        let rollout: u32 = rollout.parse()?;
        let trace = spending::trace_rollout(&execution_input, &component, rollout, |_| {
            annual_spending(rate, cut, raise)
        })?;
        let path = Path::new(output).with_extension(format!("trace-{rollout}.json"));
        serde_json::to_writer(BufWriter::new(File::create(path)?), &trace)?;
    }
    Ok(())
}
