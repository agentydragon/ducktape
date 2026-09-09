//! Experiment-owned annual targets; the engine decides the resulting funded trades.

use augur_native_invocation::{run, write_output};
use augur_rust_simulator::{
    engine::{
        SimulationError,
        allocation::{self, Observation},
    },
    ledger::AccountRef,
};

fn targets(annual_step: i64) -> impl FnMut(Observation) -> Result<Vec<i64>, SimulationError> {
    move |observation| {
        let growth = 50 + annual_step * i64::from((observation.month / 12).min(4));
        Ok(vec![growth, 100 - growth])
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    run(|input, output, parameters| {
        let [step] = parameters else {
            return Err("expected ANNUAL_STEP_PERCENT".into());
        };
        let step: i64 = step.parse()?;
        if !(0..=10).contains(&step) {
            return Err("annual step must be in [0, 10] percentage points".into());
        }
        let output_run = allocation::simulate(
            input,
            &AccountRef::new("test-retiree", "checking"),
            &(0..input.rollout_count).collect::<Vec<_>>(),
            |_| targets(step),
        )?;
        write_output(output, &output_run)
    })
}
