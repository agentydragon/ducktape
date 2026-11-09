//! Application execution that the ordered kernel does not cover yet. Each
//! item carries a `CLEANUP` tombstone naming its last caller.

mod call_transfer;
mod evaluator;
