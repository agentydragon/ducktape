use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use analysis::EffectCell;
use swc_common::sync::Lrc;
use swc_common::{DUMMY_SP, SourceMap};
use swc_ecma_ast::*;
use swc_ecma_codegen::text_writer::JsWriter;
use swc_ecma_codegen::{Config, Emitter};

use super::item_analysis::{ItemAnalysis, id_name};

#[derive(Debug, Clone)]
pub(super) enum SwappedReason {
    Export(String),
    Dependency { from: usize },
    Dependent { dependency: usize },
}

pub(super) fn format_swapped_trace(
    start: usize,
    package: &str,
    analyses: &[ItemAnalysis],
    items: &[ModuleItem],
    reasons: &BTreeMap<(usize, String), SwappedReason>,
) -> String {
    let mut node = start;
    let mut seen = BTreeSet::new();
    let mut trace = Vec::new();
    while seen.insert(node) {
        trace.push(format_item_summary(node, &analyses[node], &items[node]));
        match reasons
            .get(&(node, package.to_owned()))
            .expect("every swapped ownership has a recorded reason")
        {
            SwappedReason::Export(alias) => {
                trace.push(format!("swapped export `{alias}` from package `{package}`"));
                break;
            }
            SwappedReason::Dependency { from } => {
                trace.push(format!("dependency of item {from}"));
                node = *from;
            }
            SwappedReason::Dependent { dependency } => {
                trace.push(format!("depends on swapped item {dependency}"));
                node = *dependency;
            }
        }
    }
    trace.reverse();
    trace.join(" -> ")
}

#[derive(Debug, Clone)]
pub(super) enum LiveReason {
    ResidualExport(Vec<String>),
    HardSideEffect,
    Dependency { from: usize, via: Vec<String> },
}

impl fmt::Display for LiveReason {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LiveReason::ResidualExport(aliases) => {
                write!(f, "residual export [{}]", aliases.join(","))
            }
            LiveReason::HardSideEffect => write!(f, "retained side effect"),
            LiveReason::Dependency { from: _, via } => write!(f, "reads [{}]", via.join(",")),
        }
    }
}

pub(super) fn format_live_trace(
    start: usize,
    analyses: &[ItemAnalysis],
    items: &[ModuleItem],
    live_reasons: &[Option<LiveReason>],
) -> String {
    let mut node = start;
    let mut seen = BTreeSet::new();
    let mut trace = Vec::new();

    loop {
        if !seen.insert(node) {
            trace.push(format!("cycle at item {node}"));
            break;
        }

        trace.push(format_item_summary(node, &analyses[node], &items[node]));
        match live_reasons.get(node).and_then(|reason| reason.as_ref()) {
            Some(reason @ (LiveReason::ResidualExport(_) | LiveReason::HardSideEffect)) => {
                trace.push(reason.to_string());
                break;
            }
            Some(LiveReason::Dependency { from, via: _ }) => {
                trace.push(live_reasons[node].as_ref().unwrap().to_string());
                node = *from;
            }
            None => {
                trace.push("unknown liveness root".to_string());
                break;
            }
        }
    }

    trace.reverse();
    trace.join(" -> ")
}

pub(super) fn format_effect_cell(cell: &EffectCell) -> String {
    match cell {
        EffectCell::Binding(id) => format!("binding `{}`", id_name(id)),
        EffectCell::GlobalProp(key) => format!("global `{key}`"),
        EffectCell::ArrayContents(origin) => format!("array contents #{origin}"),
        EffectCell::SetContents(origin) => format!("set contents #{origin}"),
        EffectCell::ObjectShape(origin) => format!("object shape #{}", origin),
        EffectCell::OwnProperty { origin, property } => {
            format!("own property `{property}` of allocation #{origin}")
        }
    }
}

fn format_item_summary(i: usize, analysis: &ItemAnalysis, item: &ModuleItem) -> String {
    let declared = analysis
        .declared
        .iter()
        .map(id_name)
        .collect::<Vec<_>>()
        .join(",");
    let exports = analysis
        .export_aliases
        .iter()
        .cloned()
        .collect::<Vec<_>>()
        .join(",");
    let local_effects = analysis
        .local_effects
        .iter()
        .map(id_name)
        .collect::<Vec<_>>()
        .join(",");
    let snippet = module_item_snippet(item);
    format!(
        "item {i} declared=[{declared}] exports=[{exports}] side_effect={:?} local_effects=[{local_effects}] snippet=`{snippet}`",
        analysis.side_effect,
    )
}

pub(super) fn module_item_snippet(item: &ModuleItem) -> String {
    let module = Module {
        span: DUMMY_SP,
        body: vec![item.clone()],
        shebang: None,
    };
    let cm: Lrc<SourceMap> = Default::default();
    let mut buf = Vec::new();
    let emitted = {
        let writer = JsWriter::new(cm.clone(), "\n", &mut buf, None);
        let mut emitter = Emitter {
            cfg: Config::default(),
            cm,
            comments: None,
            wr: writer,
        };
        emitter.emit_module(&module)
    };
    if emitted.is_err() {
        return "<emit failed>".to_string();
    }
    let mut snippet = String::from_utf8_lossy(&buf)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    const MAX_LEN: usize = 240;
    if snippet.len() > MAX_LEN {
        snippet.truncate(MAX_LEN);
        snippet.push_str("...");
    }
    snippet
}
