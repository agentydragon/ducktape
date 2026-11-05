//! Small analysis/report value types shared across debundler crates.

use std::collections::BTreeSet;
use std::fmt;

use serde::{Deserialize, Serialize};

#[derive(
    Debug,
    Clone,
    Copy,
    Eq,
    PartialEq,
    Ord,
    PartialOrd,
    Hash,
    Serialize,
    Deserialize,
    strum::IntoStaticStr,
    strum::Display,
    strum::EnumString,
    strum::EnumMessage,
)]
#[cfg_attr(test, derive(strum::EnumIter))]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum DepKind {
    #[strum(message = "at-init")]
    EagerUse,
    #[strum(message = "lazy")]
    LazyUse,
    #[strum(message = "hoisted read")]
    HoistedUse,
    #[strum(message = "at-init rebind")]
    EagerRebind,
    #[strum(message = "lazy rebind")]
    LazyRebind,
    /// Rebinding write that only fires after module init (nested ≥2
    /// closures deep, or past an `await` in an async body). Rejected
    /// across destinations like the other rebinds (ESM imports are
    /// read-only whenever the write fires), but excluded from
    /// init-order constraints and the I-graph.
    #[strum(message = "deferred rebind")]
    DeferredRebind,
    #[strum(message = "side-effect")]
    Sequenced,
    /// Two statements declare the same module-scoped var binding. Emitted in
    /// both directions to require one destination without rewriting the AST.
    #[strum(message = "shared var declaration")]
    CoDeclaration,
    /// A source-prefix proof needs the producer and consumer in one module.
    #[strum(message = "stable array spread co-location")]
    CoLocation,
    #[strum(message = "local-effect")]
    LocalEffect,
}

impl DepKind {
    /// Short human-readable label used in cycle and gate diagnostics.
    pub fn diagnostic_label(self) -> &'static str {
        strum::EnumMessage::get_message(&self)
            .expect("every dependency kind has a diagnostic message")
    }
}

/// A stable direct ESM export's primitive value before import resolution.
/// Canonical shallow-scan facts; never serialized or author asserted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StableExportValue {
    /// The types the exported binding's history computes itself, joined with
    /// the values of the named/default import bindings it copies.
    Local {
        kinds: PrimKinds,
        imports: BTreeSet<String>,
    },
    /// `export { export as name } from source`.
    ReExport { source: String, export: String },
}

/// A set of ECMAScript primitive types; primitive values join by union. The
/// empty set is the result of an operation that always throws.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct PrimKinds(u8);

impl PrimKinds {
    pub const NONE: Self = Self(0);
    pub const UNDEFINED: Self = Self(1);
    pub const NULL: Self = Self(1 << 1);
    pub const BOOLEAN: Self = Self(1 << 2);
    pub const NUMBER: Self = Self(1 << 3);
    pub const BIGINT: Self = Self(1 << 4);
    pub const STRING: Self = Self(1 << 5);
    pub const SYMBOL: Self = Self(1 << 6);
    pub const ANY: Self = Self((1 << 7) - 1);

    pub const fn union(self, other: Self) -> Self {
        Self(self.0 | other.0)
    }

    pub const fn without(self, other: Self) -> Self {
        Self(self.0 & !other.0)
    }

    pub const fn is_empty(self) -> bool {
        self.0 == 0
    }

    pub const fn intersects(self, other: Self) -> bool {
        self.0 & other.0 != 0
    }

    /// Nonempty and within `other`: evaluation can complete, and when it
    /// does the value has one of `other`'s types.
    pub const fn is_only(self, other: Self) -> bool {
        self.0 != 0 && self.0 & !other.0 == 0
    }
}

impl fmt::Debug for PrimKinds {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        const NAMES: [&str; 7] = [
            "undefined",
            "null",
            "boolean",
            "number",
            "bigint",
            "string",
            "symbol",
        ];
        if *self == Self::ANY {
            return f.write_str("primitive");
        }
        let mut names = NAMES
            .iter()
            .enumerate()
            .filter(|(bit, _)| self.0 & (1 << bit) != 0)
            .map(|(_, name)| *name);
        f.write_str(names.next().unwrap_or("never"))?;
        names.try_for_each(|name| write!(f, "|{name}"))
    }
}

#[cfg(test)]
mod tests {
    use super::DepKind;
    use strum::IntoEnumIterator;

    #[test]
    fn every_dependency_kind_has_a_diagnostic_message() {
        for kind in DepKind::iter() {
            assert!(!kind.diagnostic_label().is_empty());
        }
        assert_eq!(DepKind::EagerUse.to_string(), "eager_use");
        assert_eq!(DepKind::EagerUse.diagnostic_label(), "at-init");
    }
}
