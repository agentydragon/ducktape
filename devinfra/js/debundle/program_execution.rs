//! Immutable executable definitions shared by local and imported analysis.
//!
//! Definitions contain source facts, never an execution world. Their source
//! coordinates are meaningful only together with their defining module.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

use crate::facts::sync_effects::execution_index::ExecutionIndex;

/// One source callable. A byte position alone is not a program identity:
/// independently parsed chunks can contain different functions at that position.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ProgramCallableId {
    pub module: String,
    pub source_origin: u32,
}

pub(crate) struct ExecutableModule {
    pub(crate) index: Arc<ExecutionIndex>,
    pub(crate) exports: BTreeMap<String, String>,
}

/// The provider indexes already built by canonical source preparation. Completed
/// analysis persists its results; executable definitions stay in the current SWC
/// arena and are not serialized as portable code or binding identities.
#[derive(Default)]
pub struct ProgramExecutables {
    modules: BTreeMap<String, ExecutableModule>,
}

impl fmt::Debug for ProgramExecutables {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ProgramExecutables")
            .field("modules", &self.modules.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl ProgramExecutables {
    pub(crate) fn from_modules(modules: BTreeMap<String, ExecutableModule>) -> Self {
        Self { modules }
    }

    pub(crate) fn module(&self, module: &str) -> Option<&ExecutableModule> {
        self.modules.get(module)
    }

    /// Resolve a summary's source identity against the prepared provider rather
    /// than interpreting a source offset in the importing chunk's index.
    pub(crate) fn exported_callable(
        &self,
        module: &str,
        export: &str,
        local: &str,
    ) -> Option<ProgramCallableId> {
        let program = self.module(module)?;
        if program.exports.get(export).map(String::as_str) != Some(local) {
            return None;
        }
        program
            .index
            .callable_for_local(local)
            .map(|source_origin| ProgramCallableId {
                module: module.to_owned(),
                source_origin,
            })
    }
}
