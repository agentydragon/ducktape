//! Rust oracles for emission behavior that needs exact source/AST/runtime
//! assertions and compact selector families that do not fit the corpus shape.

mod anonymous_statement;
mod chunk_renames;
mod cross_module;
mod duplicate_export;
mod import_coalescing;
mod intrinsic_alias;
mod lowering_shape;
mod rename_target_capture;
mod syntactic_holes;
mod url_rebase;
