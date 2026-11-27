use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields, untagged)]
pub(super) enum Expectation {
    Accept { accept: Accepted },
    Reject { reject: Rejected },
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Accepted {
    #[serde(default = "main_entry")]
    pub entry: String,
    pub stdout: String,
    #[serde(default)]
    pub original_stdout: Option<String>,
    #[serde(default)]
    pub check_all_js: bool,
    #[serde(default)]
    pub exports: BTreeMap<PathBuf, ExportExpectation>,
    #[serde(default)]
    pub source: BTreeMap<PathBuf, SourceExpectation>,
    #[serde(default)]
    pub probes: BTreeMap<PathBuf, Probe>,
    #[serde(default)]
    pub emitted_probes: BTreeMap<PathBuf, Probe>,
    #[serde(default)]
    pub runtime_files: BTreeMap<String, PathBuf>,
}

#[derive(Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ExportExpectation {
    #[serde(default)]
    pub include: Vec<String>,
    #[serde(default)]
    pub exclude: Vec<String>,
}

#[derive(Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct SourceExpectation {
    #[serde(default)]
    pub contains: Vec<String>,
    #[serde(default)]
    pub excludes: Vec<String>,
    #[serde(default)]
    pub occurrences_at_least: BTreeMap<String, usize>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Probe {
    pub stdout: String,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields, untagged)]
pub(super) enum Rejected {
    AtomicConflict {
        #[serde(default = "main_entry")]
        entry: String,
        atomic_conflict: AtomicConflict,
    },
    Cycle {
        #[serde(default = "main_entry")]
        entry: String,
        cycle: Cycle,
    },
    Stderr {
        #[serde(default = "main_entry")]
        entry: String,
        stderr_contains_all: Vec<String>,
    },
}

impl Rejected {
    pub(super) fn entry(&self) -> &str {
        match self {
            Self::AtomicConflict { entry, .. }
            | Self::Cycle { entry, .. }
            | Self::Stderr { entry, .. } => entry,
        }
    }
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Cycle {
    pub modules: Vec<String>,
    #[serde(default)]
    pub sequenced: bool,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct AtomicConflict {
    pub modules: Vec<String>,
    pub causes: Vec<String>,
}

fn main_entry() -> String {
    "main".to_owned()
}
