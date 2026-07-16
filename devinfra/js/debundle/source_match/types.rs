use super::*;
use std::sync::Arc;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResolvedMemberBinding {
    pub binding_name: String,
    pub kind: Option<BindingSourceKind>,
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

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct MemberBindingMatch {
    pub body_idx: usize,
    pub binding: ResolvedMemberBinding,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct MemberBindingGroupMatch {
    pub bindings: BTreeMap<String, MemberBindingMatch>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResolvedMemberBindingGroup {
    pub body_idx: usize,
    pub bindings: BTreeMap<String, ResolvedMemberBinding>,
}

/// One canonical `source_matches[].bindings[]` projection as an internal
/// source-match member selector. Each selector carries the shared source
/// pattern plus `target_binding` set to one selector-local binding.
pub struct BindingGroupMemberSelector {
    pub export_name: String,
    pub selector: AnonymousStatementSelector,
    pub parsed_selector: ParsedSourceMatchSelector,
    pub comment: Option<String>,
    pub note: Option<String>,
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
