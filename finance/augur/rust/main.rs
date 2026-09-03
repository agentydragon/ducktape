use std::{
    env,
    fs::File,
    io::{BufReader, BufWriter},
};

use augur_rust_simulator::{Fixture, simulate};

fn main() {
    if let Err(error) = run() {
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args_os();
    let program = args.next().unwrap_or_default();
    let input = args.next().ok_or_else(|| {
        format!(
            "usage: {} FIXTURE.json OUTPUT.json",
            program.to_string_lossy()
        )
    })?;
    let output = args.next().ok_or_else(|| {
        format!(
            "usage: {} FIXTURE.json OUTPUT.json",
            program.to_string_lossy()
        )
    })?;
    if args.next().is_some() {
        return Err(format!(
            "usage: {} FIXTURE.json OUTPUT.json",
            program.to_string_lossy()
        )
        .into());
    }
    let fixture: Fixture = serde_json::from_reader(BufReader::new(File::open(input)?))?;
    let result = simulate(&fixture)?;
    serde_json::to_writer(BufWriter::new(File::create(output)?), &result)?;
    Ok(())
}
