//! Synthetic source_match workload for profiling declaration-hole selectors.
//!
//! The generated program is intentionally generic: many top-level `const`
//! declarations, many declarators per declaration, and many selectors shaped
//! like a direct CSS/literal sweep:
//!
//! ```js
//! const DECLARATORS_BEFORE = null,
//!   selected = STR_LITERAL_MATCHING_RE("^generic-token-123$"),
//!   DECLARATORS_AFTER = null;
//! ```
//!
//! Run through Bazel and a sampler, for example:
//!
//! ```bash
//! bazelisk run //devinfra/js/debundle/perf:source_match_decl_holes_profile -- \
//!   --mode binding-group --declarations 600 --declarators 10 --selectors 600 --repetitions 1
//! ```

use std::collections::{BTreeMap, BTreeSet};
use std::time::Instant;

use spec::{AnonymousStatementSelector, SourceMatchIdentifierMode};

#[derive(Debug)]
struct Opts {
    declarations: usize,
    declarators: usize,
    selectors: usize,
    repetitions: usize,
    mode: Mode,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Mode {
    /// Select one pinned declarator by `target_binding`.
    TargetBinding,
    /// Omit `target_binding`, so matching the wider declaration produces
    /// the same multi-binding ambiguity diagnostic shape as member-form
    /// direct-selector sweeps.
    Ambiguity,
    /// Resolve through `binding_groups[].source_match` with one exported
    /// target binding, the preferred spec shape for direct literal sweeps.
    BindingGroup,
}

impl Default for Opts {
    fn default() -> Self {
        Self {
            declarations: 600,
            declarators: 10,
            selectors: 600,
            repetitions: 1,
            mode: Mode::BindingGroup,
        }
    }
}

fn main() -> anyhow::Result<()> {
    let opts = parse_opts()?;
    js_ast::with_swc_globals(|| run(opts))
}

fn run(opts: Opts) -> anyhow::Result<()> {
    if opts.declarations == 0 || opts.declarators == 0 || opts.selectors == 0 {
        anyhow::bail!("declarations, declarators, and selectors must all be non-zero");
    }
    let runtime_source = runtime_source(&opts);
    let runtime_module =
        js_ast::parse_js_module_ast("<synthetic source_match profile>", &runtime_source)?;
    let selectors = selectors(&opts);

    let started = Instant::now();
    let mut checksum = 0usize;
    for _ in 0..opts.repetitions {
        for (selector_idx, selector) in selectors.iter().enumerate() {
            match opts.mode {
                Mode::TargetBinding | Mode::Ambiguity => {
                    let result = source_match::resolve_member_binding(
                        &runtime_module,
                        "synthetic/source_match_profile",
                        &format!("export_{selector_idx}"),
                        selector,
                    );
                    match (opts.mode, result) {
                        (Mode::TargetBinding, Ok(resolved)) => {
                            checksum = checksum.wrapping_add(resolved.binding_name.len());
                        }
                        (Mode::TargetBinding, Err(err)) => return Err(err),
                        (Mode::Ambiguity, Ok(resolved)) => {
                            checksum = checksum.wrapping_add(resolved.binding_name.len());
                        }
                        (Mode::Ambiguity, Err(err)) => {
                            checksum = checksum.wrapping_add(err.to_string().len());
                        }
                        (Mode::BindingGroup, _) => unreachable!("outer match excludes this mode"),
                    }
                }
                Mode::BindingGroup => {
                    let resolved = source_match::resolve_member_binding_group(
                        &runtime_module,
                        "synthetic/source_match_profile",
                        selector,
                        &BTreeMap::from([("selected".to_string(), "selected".to_string())]),
                    )?;
                    checksum = checksum.wrapping_add(
                        resolved
                            .values()
                            .map(|binding| binding.binding_name.len())
                            .sum::<usize>(),
                    );
                }
            }
        }
    }
    let elapsed = started.elapsed();
    println!(
        "source_match_decl_holes_profile mode={:?} declarations={} declarators={} selectors={} repetitions={} elapsed_ms={} checksum={}",
        opts.mode,
        opts.declarations,
        opts.declarators,
        opts.selectors,
        opts.repetitions,
        elapsed.as_millis(),
        checksum,
    );
    Ok(())
}

fn parse_opts() -> anyhow::Result<Opts> {
    let mut opts = Opts::default();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let value = args
            .next()
            .ok_or_else(|| anyhow::anyhow!("{arg} requires a value"))?;
        match arg.as_str() {
            "--declarations" => opts.declarations = parse_usize(&arg, &value)?,
            "--declarators" => opts.declarators = parse_usize(&arg, &value)?,
            "--selectors" => opts.selectors = parse_usize(&arg, &value)?,
            "--repetitions" => opts.repetitions = parse_usize(&arg, &value)?,
            "--mode" => {
                opts.mode = match value.as_str() {
                    "target-binding" => Mode::TargetBinding,
                    "ambiguity" => Mode::Ambiguity,
                    "binding-group" => Mode::BindingGroup,
                    _ => anyhow::bail!(
                        "--mode expects `target-binding`, `ambiguity`, or `binding-group`, got {value:?}"
                    ),
                };
            }
            _ => anyhow::bail!("unknown argument {arg}"),
        }
    }
    Ok(opts)
}

fn parse_usize(arg: &str, value: &str) -> anyhow::Result<usize> {
    value
        .parse::<usize>()
        .map_err(|err| anyhow::anyhow!("{arg} expects a usize, got {value:?}: {err}"))
}

fn runtime_source(opts: &Opts) -> String {
    let mut source = String::new();
    for decl_idx in 0..opts.declarations {
        source.push_str("const ");
        for declarator_idx in 0..opts.declarators {
            if declarator_idx > 0 {
                source.push_str(",\n  ");
            }
            source.push_str(&format!(
                "runtime_{decl_idx}_{declarator_idx} = \"{}\"",
                literal_for(decl_idx, declarator_idx),
            ));
        }
        source.push_str(";\n");
    }
    source
}

fn selectors(opts: &Opts) -> Vec<AnonymousStatementSelector> {
    (0..opts.selectors)
        .map(|selector_idx| {
            let decl_idx = selector_idx % opts.declarations;
            let declarator_idx = (selector_idx / opts.declarations) % opts.declarators;
            AnonymousStatementSelector {
                match_source: format!(
                    "const DECLARATORS_BEFORE = null,\n  selected = STR_LITERAL_MATCHING_RE(\"^{}$\"),\n  DECLARATORS_AFTER = null;",
                    literal_for(decl_idx, declarator_idx),
                ),
                identifiers: SourceMatchIdentifierMode::AlphaAll,
                target_binding: (opts.mode == Mode::TargetBinding).then(|| "selected".to_string()),
                target_statement: None,
                target_statements: None,
                wildcard_string_literals: BTreeSet::new(),
            }
        })
        .collect()
}

fn literal_for(decl_idx: usize, declarator_idx: usize) -> String {
    format!("generic-token-{decl_idx:04}-{declarator_idx:02}")
}
