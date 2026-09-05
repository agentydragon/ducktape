//! Clean-sheet deterministic Augur simulator.
//!
//! Money is represented exclusively as integer currency quanta. The crate has
//! no floating-point monetary type and does not deserialize JSON numbers into
//! one.

pub mod allocation;
pub mod engine;
pub mod event_frames;
pub mod fixture;
pub mod ledger;
pub mod money;
pub mod product;
pub mod tax;

pub use engine::{
    SimulationError, ValidatedFixture, simulate, simulate_dense, simulate_dense_validated,
    simulate_product_metrics, simulate_product_metrics_validated, simulate_summaries,
    simulate_summaries_validated, simulate_validated,
};
pub use event_frames::{EventFrames, FramedOutput};
pub use fixture::{Fixture, PopulationOutput, SimulationOutput};
pub use product::{BASE_METRIC_NAMES, ProductMetricSeries};
