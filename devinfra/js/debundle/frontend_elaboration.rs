//! Shared source-only preparation, selector solving, and premise collection.

#[path = "frontend_elaboration/chunk_ast.rs"]
pub mod chunk_ast;
#[path = "frontend_elaboration/elaboration.rs"]
mod elaboration;
#[path = "frontend_elaboration/source_ast.rs"]
pub mod source_ast;

pub use elaboration::chunk_preparation::eligible_member_indices;
pub use elaboration::member_claims::{MemberClaimInput, project_member_claims};
pub use elaboration::resolved_ownership::{
    CanonicalOwnership, ClaimSelector, OwnershipClaim, resolve_ownership,
};
pub use elaboration::source_sites::select_one_declared_binding;
pub use elaboration::{
    FrontendChunk, FrontendChunkInput, FrontendElaboration, FrontendSpecInputs,
    MemberClaimProjection, ResolvedMemberSite, complete_analysis_spec, elaborate, project_analysis,
};
