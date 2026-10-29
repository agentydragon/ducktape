//! Heap writes a builtin performs.

use super::builtins::ArgSel;

/// A write a builtin row performs. Writes to the fresh allocation are its
/// initial contents; the row's `allocates` names that allocation's kind.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mutation {
    /// `SetIntegrityLevel(O, frozen)` on the argument: it becomes
    /// non-extensible with every own property non-configurable and, for data
    /// properties, non-writable.
    Integrity(ArgSel),
    /// `[[DefineOwnProperty]]` on `target` for every own enumerable key of the
    /// `descriptors` record.
    DefineProperties { target: ArgSel, descriptors: ArgSel },
    /// The fresh allocation receives the argument's own property descriptors:
    /// data values and accessor functions by identity, never invoked.
    FreshOwnDescriptorsOf(ArgSel),
    /// The fresh allocation receives the argument's own string keys as dense
    /// elements in `[[OwnPropertyKeys]]` order.
    FreshOwnKeysOf(ArgSel),
    /// An internal slot of the fresh allocation retains the argument without
    /// invoking it.
    FreshRetains(ArgSel),
}
