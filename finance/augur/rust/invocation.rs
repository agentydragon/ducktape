//! File transport for experiment-owned native entrypoints. No policy or capture choice.
//!
//! The shared prefix is INPUT.json OUTPUT.json; the experiment interprets all remaining
//! arguments and calls its chosen engine entrypoint, which owns financial validation.

use std::{
    env,
    error::Error,
    fs::File,
    io::{BufReader, BufWriter, Write},
    path::Path,
};

use augur_rust_simulator::execution::ExecutionInput;
use serde::Serialize;

pub fn run(
    execute: impl FnOnce(&ExecutionInput, &Path, &[String]) -> Result<(), Box<dyn Error>>,
) -> Result<(), Box<dyn Error>> {
    let args: Vec<_> = env::args().skip(1).collect();
    let [input, output, parameters @ ..] = args.as_slice() else {
        return Err("usage: runner EXECUTION-INPUT.json OUTPUT.json [EXPERIMENT_ARGS ...]".into());
    };
    let input: ExecutionInput = serde_json::from_reader(BufReader::new(File::open(input)?))?;
    execute(&input, Path::new(output), parameters)
}

/// Primary results and selected traces share the same checked output boundary.
pub fn write_output(path: &Path, output: &impl Serialize) -> Result<(), Box<dyn Error>> {
    let mut writer = BufWriter::new(File::create(path)?);
    serde_json::to_writer(&mut writer, output)?;
    writer.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn buffered_output_failure_is_not_reported_as_success() {
        // Linux's full device accepts the open, then rejects the small buffered write.
        assert!(write_output(Path::new("/dev/full"), &vec![1, 2, 3]).is_err());
    }
}
