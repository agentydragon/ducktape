//! Formal retained graph of one exact provider execution. Slots preserve alias
//! and lexical-cell identity; they are not runtime allocation addresses or an
//! own-property certificate. The initial importer keeps the graph opaque.

use super::{ParameterHeapRead, RecordInputProperty, RecordInputShape};
use analysis_model::PrimKinds;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub(crate) enum ProviderGraphInput {
    Argument { index: usize },
    Property { argument: usize, property: String },
    Getter { argument: usize, property: String },
    Setter { argument: usize, property: String },
}

impl ProviderGraphInput {
    fn is_valid(&self, shape: &RecordInputShape, reads: &BTreeSet<ParameterHeapRead>) -> bool {
        match self {
            Self::Argument { index } => *index == 0,
            Self::Property { argument, property } => {
                *argument == 0
                    && matches!(
                        shape.properties.get(property),
                        Some(RecordInputProperty::Data)
                    )
                    && reads.contains(&ParameterHeapRead::ObjectShape { argument: 0 })
                    && reads.contains(&ParameterHeapRead::OwnProperty {
                        argument: 0,
                        property: property.clone(),
                    })
            }
            Self::Getter { argument, property } => {
                *argument == 0
                    && matches!(
                        shape.properties.get(property),
                        Some(RecordInputProperty::Accessor { getter: true, .. })
                    )
                    && reads.contains(&ParameterHeapRead::ObjectShape { argument: 0 })
            }
            Self::Setter { argument, property } => {
                *argument == 0
                    && matches!(
                        shape.properties.get(property),
                        Some(RecordInputProperty::Accessor { setter: true, .. })
                    )
                    && reads.contains(&ParameterHeapRead::ObjectShape { argument: 0 })
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub(crate) enum ProviderGraphValue {
    Input { input: ProviderGraphInput },
    Primitive { kinds: PrimKinds },
    Slot { slot: usize },
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub(crate) enum ProviderGraphNodeKind {
    Record,
    Array,
    DescriptorMap,
    /// Provider-source metadata only; never resolve this key in caller SWC.
    Closure {
        source_key: u32,
    },
    LexicalCell,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub(crate) enum ProviderGraphRole {
    OwnData {
        property: String,
    },
    Getter {
        property: String,
    },
    Setter {
        property: String,
    },
    ArrayElement {
        index: usize,
    },
    ClosureCell {
        index: usize,
    },
    CellValue,
    /// Explicit additional roots retained by the shared producer. This grants
    /// no property/value relation beyond reachability.
    Retained,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub(crate) struct ProviderGraphEdge {
    pub(crate) role: ProviderGraphRole,
    pub(crate) value: ProviderGraphValue,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub(crate) struct ProviderGraphNode {
    pub(crate) kind: ProviderGraphNodeKind,
    pub(crate) edges: BTreeSet<ProviderGraphEdge>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub(crate) struct ProviderRetainedGraph {
    pub(crate) roots: BTreeSet<ProviderGraphValue>,
    pub(crate) nodes: Vec<ProviderGraphNode>,
}

impl ProviderRetainedGraph {
    pub(crate) fn inputs(&self) -> BTreeSet<ProviderGraphInput> {
        self.roots
            .iter()
            .chain(
                self.nodes
                    .iter()
                    .flat_map(|node| node.edges.iter().map(|edge| &edge.value)),
            )
            .filter_map(|value| match value {
                ProviderGraphValue::Input { input } => Some(input.clone()),
                _ => None,
            })
            .collect()
    }

    pub(crate) fn is_well_formed(
        &self,
        shape: &RecordInputShape,
        reads: &BTreeSet<ParameterHeapRead>,
    ) -> bool {
        if self.roots.is_empty() || self.nodes.is_empty() || self.nodes.len() > 512 {
            return false;
        }
        let valid_value = |value: &ProviderGraphValue| match value {
            ProviderGraphValue::Input { input } => input.is_valid(shape, reads),
            ProviderGraphValue::Primitive { kinds } => {
                !kinds.is_empty() && kinds.without(PrimKinds::ANY).is_empty()
            }
            ProviderGraphValue::Slot { slot } => *slot < self.nodes.len(),
        };
        if !self.roots.iter().all(valid_value) || self.nodes.iter().any(|node| node.edges.iter().any(|edge| {
            if !valid_value(&edge.value) { return true; }
            match (&node.kind, &edge.role) {
                (_, ProviderGraphRole::Retained) => false,
                (ProviderGraphNodeKind::Record | ProviderGraphNodeKind::Array | ProviderGraphNodeKind::DescriptorMap,
                    ProviderGraphRole::OwnData { .. } | ProviderGraphRole::Getter { .. } | ProviderGraphRole::Setter { .. }) => false,
                (ProviderGraphNodeKind::Array, ProviderGraphRole::ArrayElement { .. }) => false,
                (ProviderGraphNodeKind::LexicalCell, ProviderGraphRole::CellValue) => false,
                (ProviderGraphNodeKind::Closure { .. }, ProviderGraphRole::ClosureCell { .. }) => !matches!(&edge.value,
                    ProviderGraphValue::Slot { slot } if matches!(self.nodes[*slot].kind, ProviderGraphNodeKind::LexicalCell)),
                _ => true,
            }
        })) { return false; }
        let mut pending: Vec<_> = self.roots.iter().collect();
        let mut reached = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if let ProviderGraphValue::Slot { slot } = value
                && reached.insert(*slot)
            {
                pending.extend(self.nodes[*slot].edges.iter().map(|edge| &edge.value));
            }
        }
        reached.len() == self.nodes.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slots_and_descriptor_inputs_are_checked_as_one_graph() {
        let shape = RecordInputShape {
            properties: [(
                "handler".into(),
                RecordInputProperty::Accessor {
                    getter: true,
                    setter: false,
                },
            )]
            .into(),
            creation_order: vec!["handler".into()],
        };
        let reads = BTreeSet::from([ParameterHeapRead::ObjectShape { argument: 0 }]);
        let mut graph = ProviderRetainedGraph {
            roots: BTreeSet::from([ProviderGraphValue::Slot { slot: 0 }]),
            nodes: vec![ProviderGraphNode {
                kind: ProviderGraphNodeKind::Record,
                edges: BTreeSet::from([
                    ProviderGraphEdge {
                        role: ProviderGraphRole::OwnData {
                            property: "self".into(),
                        },
                        value: ProviderGraphValue::Slot { slot: 0 },
                    },
                    ProviderGraphEdge {
                        role: ProviderGraphRole::Getter {
                            property: "handler".into(),
                        },
                        value: ProviderGraphValue::Input {
                            input: ProviderGraphInput::Getter {
                                argument: 0,
                                property: "handler".into(),
                            },
                        },
                    },
                ]),
            }],
        };
        assert!(
            graph.is_well_formed(&shape, &reads),
            "cycles preserve slot identity"
        );
        assert!(
            !graph.is_well_formed(&shape, &BTreeSet::new()),
            "descriptor roots require their caller shape read"
        );
        let valid = graph.clone();
        graph.nodes[0].edges.insert(ProviderGraphEdge {
            role: ProviderGraphRole::Setter {
                property: "handler".into(),
            },
            value: ProviderGraphValue::Input {
                input: ProviderGraphInput::Setter {
                    argument: 0,
                    property: "handler".into(),
                },
            },
        });
        assert!(
            !graph.is_well_formed(&shape, &reads),
            "an absent setter cannot be substituted"
        );
        graph = valid.clone();
        graph.nodes[0].edges.insert(ProviderGraphEdge {
            role: ProviderGraphRole::Retained,
            value: ProviderGraphValue::Slot { slot: 1 },
        });
        assert!(
            !graph.is_well_formed(&shape, &reads),
            "dangling slots cannot lose roots"
        );
        graph = valid;
        graph.nodes.push(ProviderGraphNode {
            kind: ProviderGraphNodeKind::Record,
            edges: BTreeSet::new(),
        });
        assert!(
            !graph.is_well_formed(&shape, &reads),
            "unrelated touched objects are not result graph nodes"
        );
    }
}
