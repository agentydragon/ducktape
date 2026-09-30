use super::*;
use std::sync::Arc;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResolvedMemberBinding {
    pub binding_name: String,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct SourceMatchNearMiss {
    pub body_idx: usize,
    pub declared_bindings: Vec<String>,
    pub score: usize,
    pub reason: String,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SourceMatchBodyDebt {
    pub exact_groups: Vec<Vec<Option<usize>>>,
    pub near_misses: Vec<SourceMatchNearMiss>,
}

/// `free_bindings` on every candidate maps each of the template's free
/// identifiers (`free_identifiers`) to the chunk identifier it bound to in that
/// match.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct MemberBindingMatch {
    pub body_idx: usize,
    pub binding: ResolvedMemberBinding,
    pub free_bindings: BTreeMap<String, String>,
}

/// A binding claimed at the top-level statement that declares it.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct MatchedBinding {
    pub body_idx: usize,
    pub binding: ResolvedMemberBinding,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct MemberBindingGroupMatch {
    pub bindings: BTreeMap<String, MatchedBinding>,
    pub free_bindings: BTreeMap<String, String>,
}

/// One anonymous-statement candidate: the matched top-level body indices, one
/// per template statement.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct AnonymousGroupMatch {
    pub body_indices: Vec<usize>,
    pub free_bindings: BTreeMap<String, String>,
}

/// One canonical `source_matches[].bindings[]` projection as an internal
/// source-match member selector. Each selector carries the shared source
/// pattern plus `target_binding` set to one selector-local binding.
pub struct BindingGroupMemberSelector {
    pub export_name: String,
    pub parsed_selector: ParsedSourceMatchSelector,
}

#[derive(Clone)]
pub struct ParsedSourceMatchSelector {
    selector: AnonymousStatementSelector,
    parsed: Arc<Module>,
}

impl std::fmt::Debug for ParsedSourceMatchSelector {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ParsedSourceMatchSelector")
            .field("selector", &self.selector)
            .field("body_len", &self.parsed.body.len())
            .finish()
    }
}

impl ParsedSourceMatchSelector {
    pub(crate) fn new(selector: AnonymousStatementSelector, parsed: Module) -> Self {
        Self {
            selector,
            parsed: Arc::new(parsed),
        }
    }

    pub fn selector(&self) -> &AnonymousStatementSelector {
        &self.selector
    }

    pub fn body(&self) -> &[ModuleItem] {
        &self.parsed.body
    }

    pub fn with_target_binding(&self, target_binding: Option<String>) -> Self {
        let mut selector = self.selector.clone();
        selector.target_binding = target_binding;
        Self {
            selector,
            parsed: Arc::clone(&self.parsed),
        }
    }

    pub fn declared_binding_names(&self) -> Vec<String> {
        self.body()
            .iter()
            .flat_map(declared_bindings)
            .map(|binding| binding.binding_name)
            .collect()
    }
}
