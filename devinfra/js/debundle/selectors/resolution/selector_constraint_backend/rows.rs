//! Flat, interned allowed-tuple row storage.

use std::hash::{Hash, Hasher};

use crate::BackendValueId;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledAllowedTupleRows {
    arity: usize,
    values: Vec<BackendValueId>,
}

impl CompiledAllowedTupleRows {
    pub(crate) fn from_rows(arity: usize, rows: Vec<Vec<BackendValueId>>) -> Self {
        debug_assert!(arity > 0);
        let mut values = Vec::with_capacity(rows.len().saturating_mul(arity));
        for row in rows {
            debug_assert_eq!(row.len(), arity);
            values.extend(row);
        }
        Self { arity, values }
    }

    pub fn from_flat_rows(arity: usize, mut values: Vec<BackendValueId>) -> Self {
        debug_assert!(arity > 0);
        sort_dedup_flat_rows(arity, &mut values);
        Self { arity, values }
    }

    pub(crate) fn from_binary_rows(rows: Vec<(BackendValueId, BackendValueId)>) -> Self {
        let mut values = Vec::with_capacity(rows.len().saturating_mul(2));
        for (left, right) in rows {
            values.push(left);
            values.push(right);
        }
        Self { arity: 2, values }
    }

    pub fn len(&self) -> usize {
        debug_assert!(self.arity > 0);
        self.values.len() / self.arity
    }

    pub fn arity(&self) -> usize {
        self.arity
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    pub fn cell_count(&self) -> usize {
        self.values.len()
    }

    pub fn values(&self) -> &[BackendValueId] {
        &self.values
    }

    pub fn iter(&self) -> impl Iterator<Item = &[BackendValueId]> {
        debug_assert!(self.arity > 0);
        self.values.chunks_exact(self.arity)
    }

    pub fn contains(&self, row: &[BackendValueId]) -> bool {
        self.iter().any(|candidate| candidate == row)
    }

    pub(crate) fn fingerprint(&self) -> AllowedTupleRowsFingerprint {
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        self.arity.hash(&mut hasher);
        self.values.hash(&mut hasher);
        AllowedTupleRowsFingerprint {
            arity: self.arity,
            cell_count: self.values.len(),
            hash: hasher.finish(),
        }
    }
}

fn sort_dedup_flat_rows(arity: usize, values: &mut Vec<BackendValueId>) {
    debug_assert!(arity > 0);
    if values.is_empty() {
        return;
    }
    debug_assert_eq!(values.len() % arity, 0);
    let row_count = values.len() / arity;
    let mut row_indices = (0..row_count).collect::<Vec<_>>();
    row_indices.sort_unstable_by(|left, right| {
        let left_start = left * arity;
        let right_start = right * arity;
        values[left_start..left_start + arity].cmp(&values[right_start..right_start + arity])
    });

    let mut deduped = Vec::with_capacity(values.len());
    for row_index in row_indices {
        let row_start = row_index * arity;
        let row = &values[row_start..row_start + arity];
        if deduped.len() >= arity && &deduped[deduped.len() - arity..] == row {
            continue;
        }
        deduped.extend_from_slice(row);
    }
    *values = deduped;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) struct AllowedTupleRowsFingerprint {
    arity: usize,
    cell_count: usize,
    hash: u64,
}
