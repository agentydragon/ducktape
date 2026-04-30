use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;

mod ast_ir;
mod emit;
mod owner_graph;
mod pipeline;
mod plan;

use pipeline::{Cli, run};

#[derive(Parser, Debug)]
struct Args {
    #[arg(long)]
    input_root: PathBuf,
    #[arg(long)]
    js_list: PathBuf,
    #[arg(long)]
    out_root: PathBuf,
}

fn main() -> Result<()> {
    let args = Args::parse();
    run(&Cli {
        input_root: args.input_root,
        js_list: args.js_list,
        out_root: args.out_root,
    })
}
