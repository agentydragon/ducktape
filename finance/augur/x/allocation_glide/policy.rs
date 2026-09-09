//! Experiment-owned annual targets; the engine decides the resulting funded trades.

use std::{
    env,
    fs::File,
    io::{BufReader, BufWriter},
};

use augur_rust_simulator::{
    engine::{
        SimulationError,
        allocation::{self, Observation},
    },
    execution::ExecutionInput,
    ledger::AccountRef,
};

fn targets(annual_step: i64) -> impl FnMut(Observation) -> Result<Vec<i64>, SimulationError> {
    move |observation| {
        let growth = 50 + annual_step * i64::from((observation.month / 12).min(4));
        Ok(vec![growth, 100 - growth])
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args().skip(1).collect();
    let [input, output, step] = args.as_slice() else {
        return Err("usage: runner EXECUTION-INPUT.json OUTPUT.json ANNUAL_STEP_PERCENT".into());
    };
    let step: i64 = step.parse()?;
    if !(0..=10).contains(&step) {
        return Err("annual step must be in [0, 10] percentage points".into());
    }
    let input: ExecutionInput = serde_json::from_reader(BufReader::new(File::open(input)?))?;
    let output_run = allocation::simulate(
        &input,
        &AccountRef::new("test-retiree", "checking"),
        &(0..input.rollout_count).collect::<Vec<_>>(),
        |_| targets(step),
    )?;
    serde_json::to_writer(BufWriter::new(File::create(output)?), &output_run)?;
    Ok(())
}
