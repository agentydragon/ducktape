//! Shared source-only preparation, selector solving, and premise collection.

#[path = "frontend_elaboration/chunk_ast.rs"]
pub mod chunk_ast;
#[path = "frontend_elaboration/elaboration.rs"]
mod elaboration;
#[path = "frontend_elaboration/source_ast.rs"]
pub mod source_ast;

pub use elaboration::{
    FrontendChunk, FrontendChunkInput, FrontendElaboration, FrontendSpecInputs, MemberClaimInput,
    MemberClaimProjection, ResolvedMemberSite, complete_analysis_spec, elaborate,
    eligible_member_indices, project_analysis, project_member_claims, select_one_declared_binding,
};
