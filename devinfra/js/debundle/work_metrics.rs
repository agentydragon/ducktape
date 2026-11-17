//! Invocation-owned, bounded-cardinality work accounting. The solver owns its
//! batch; SDK aggregation and export happen only at batch/phase boundaries.

use opentelemetry::KeyValue;
use opentelemetry::metrics::{Counter, Gauge, Meter};
use std::sync::Arc;

macro_rules! categories {
    ($name:ident { $($variant:ident),+ $(,)? }) => {
        #[derive(Clone, Copy, Debug, strum::EnumCount, strum::FromRepr, strum::IntoStaticStr)]
        #[strum(serialize_all = "snake_case")]
        #[repr(usize)]
        pub enum $name { $($variant),+ }
        impl $name {
            const COUNT: usize = <Self as strum::EnumCount>::COUNT;
            fn label(index: usize) -> &'static str {
                Self::from_repr(index).expect("metric category index").into()
            }
        }
    };
}

categories!(Operation {
    Read,
    AbsentRead,
    NewFact,
    ChangedProposal,
    UnchangedProposal,
    NewSubscription,
    RepeatedSubscription,
    WakeAttempt,
    CoalescedWake,
    Schedule,
    BitmapPointerSkip,
    BitmapSubsetSkip,
    BitmapSupersetAdoption,
    BitmapCopyOnWriteUnion,
});

categories!(KeyFamily {
    Other,
    Active,
    Binding,
    CandidateBinding,
    CandidateReachable,
    Property,
    Properties,
    Return,
    Parameters,
    ReadyParameters,
    InvokedCells,
    RetainedCells,
    SourceCells,
    Initialization,
    ReadOnlyCallback,
});

categories!(EventKind {
    Other,
    Initialization,
    FunctionDeclaration,
    ClassDeclaration,
    Variable,
    AssignBinding,
    AssignProperty,
    WritePropertyExpr,
    WritePropertyBinding,
    WritePropertyValue,
    FunctionMetadata,
    Return,
    Invoke,
});

struct Instruments {
    operations: Counter<u64>,
    reads: Counter<u64>,
    absent_reads: Counter<u64>,
    evaluations: Counter<u64>,
    fact_rows: Gauge<u64>,
    subscriptions: Gauge<u64>,
    pending: Gauge<u64>,
    views: Counter<u64>,
    owned_declarators: Counter<u64>,
    frontend_chunks: Counter<u64>,
    initializer_validations: Counter<u64>,
    operation_attributes: [[KeyValue; 1]; Operation::COUNT],
    family_attributes: [[KeyValue; 1]; KeyFamily::COUNT],
    event_attributes: [[[KeyValue; 2]; 2]; EventKind::COUNT],
}

/// Disabled by default. No SDK objects or batch storage are created without a
/// meter; a command's provider is never installed as a process-global provider.
#[derive(Clone, Default)]
pub struct WorkMetrics(Option<Arc<Instruments>>);

impl WorkMetrics {
    pub fn enabled(&self) -> bool {
        self.0.is_some()
    }

    pub fn new(meter: Option<Meter>) -> Self {
        Self(meter.map(|meter| {
            Arc::new(Instruments {
                operations: meter.u64_counter("debundle.inference.operations").build(),
                reads: meter.u64_counter("debundle.inference.reads").build(),
                absent_reads: meter.u64_counter("debundle.inference.absent_reads").build(),
                evaluations: meter.u64_counter("debundle.inference.evaluations").build(),
                fact_rows: meter.u64_gauge("debundle.inference.fact_rows").build(),
                subscriptions: meter.u64_gauge("debundle.inference.subscriptions").build(),
                pending: meter.u64_gauge("debundle.inference.pending").build(),
                views: meter
                    .u64_counter("debundle.frontend.statement_views")
                    .build(),
                owned_declarators: meter
                    .u64_counter("debundle.frontend.owned_declarators")
                    .build(),
                frontend_chunks: meter
                    .u64_counter("debundle.frontend.chunks_prepared")
                    .build(),
                initializer_validations: meter
                    .u64_counter("debundle.frontend.initializer_validations")
                    .build(),
                operation_attributes: std::array::from_fn(|i| {
                    [KeyValue::new("operation", Operation::label(i))]
                }),
                family_attributes: std::array::from_fn(|i| {
                    [KeyValue::new("family", KeyFamily::label(i))]
                }),
                event_attributes: std::array::from_fn(|i| {
                    std::array::from_fn(|repeated| {
                        [
                            KeyValue::new("event", EventKind::label(i)),
                            KeyValue::new(
                                "execution",
                                if repeated == 0 { "initial" } else { "repeated" },
                            ),
                        ]
                    })
                }),
            })
        }))
    }

    pub fn batch(&self) -> Option<Batch> {
        self.0.as_ref().map(|instruments| Batch {
            instruments: instruments.clone(),
            counts: [0; Operation::COUNT],
            reads: [0; KeyFamily::COUNT],
            absent_reads: [0; KeyFamily::COUNT],
            rows: [0; KeyFamily::COUNT],
            executions: [[0; 2]; EventKind::COUNT],
            subscriptions: 0,
            pending: 0,
            unpublished: 0,
        })
    }

    /// Count existing prepared views without walking/cloning their ASTs.
    pub fn statement_views(&self, count: usize, owned: usize) {
        if let Some(instruments) = &self.0 {
            instruments.views.add(count as u64, &[]);
            instruments.owned_declarators.add(owned as u64, &[]);
        }
    }

    pub fn frontend_chunk(&self) {
        if let Some(instruments) = &self.0 {
            instruments.frontend_chunks.add(1, &[]);
        }
    }

    pub fn initializer_validation(&self) {
        if let Some(instruments) = &self.0 {
            instruments.initializer_validations.add(1, &[]);
        }
    }
}

/// A single worker's fixed-size counters. Between flush boundaries, updates
/// need no locks, allocation, clock reads or attribute creation.
pub struct Batch {
    instruments: Arc<Instruments>,
    counts: [u64; Operation::COUNT],
    reads: [u64; KeyFamily::COUNT],
    absent_reads: [u64; KeyFamily::COUNT],
    rows: [u64; KeyFamily::COUNT],
    executions: [[u64; 2]; EventKind::COUNT],
    subscriptions: u64,
    pending: u64,
    unpublished: usize,
}

impl Batch {
    pub fn add(&mut self, operation: Operation, count: u64) {
        self.counts[operation as usize] += count;
        self.unpublished += 1;
        if self.unpublished >= 4096 {
            self.flush();
        }
    }

    pub fn read(&mut self, family: KeyFamily, absent: bool) {
        self.reads[family as usize] += 1;
        self.add(Operation::Read, 1);
        if absent {
            self.absent_reads[family as usize] += 1;
            self.add(Operation::AbsentRead, 1);
        }
    }

    pub fn new_fact(&mut self, family: KeyFamily) {
        self.rows[family as usize] += 1;
        self.add(Operation::NewFact, 1);
    }

    pub fn execution(&mut self, event: EventKind, repeated: bool) {
        self.executions[event as usize][usize::from(repeated)] += 1;
        self.unpublished += 1;
        if self.unpublished >= 4096 {
            self.flush();
        }
    }

    pub fn subscription(&mut self, added: bool) {
        self.subscriptions += u64::from(added);
        self.add(
            if added {
                Operation::NewSubscription
            } else {
                Operation::RepeatedSubscription
            },
            1,
        );
    }

    pub fn pending(&mut self, count: usize) {
        self.pending = count as u64;
    }

    /// Publish only fixed-size scalars. Never traverse fact tables or bitmaps
    /// from the SDK reader thread to obtain a snapshot.
    pub fn flush(&mut self) {
        let instruments = &self.instruments;
        for (i, count) in self.counts.iter_mut().enumerate() {
            if *count != 0 {
                instruments
                    .operations
                    .add(std::mem::take(count), &instruments.operation_attributes[i]);
            }
        }
        for i in 0..KeyFamily::COUNT {
            if self.reads[i] != 0 {
                instruments.reads.add(
                    std::mem::take(&mut self.reads[i]),
                    &instruments.family_attributes[i],
                );
            }
            if self.absent_reads[i] != 0 {
                instruments.absent_reads.add(
                    std::mem::take(&mut self.absent_reads[i]),
                    &instruments.family_attributes[i],
                );
            }
            instruments
                .fact_rows
                .record(self.rows[i], &instruments.family_attributes[i]);
        }
        for i in 0..EventKind::COUNT {
            for repeated in 0..2 {
                if self.executions[i][repeated] != 0 {
                    instruments.evaluations.add(
                        std::mem::take(&mut self.executions[i][repeated]),
                        &instruments.event_attributes[i][repeated],
                    );
                }
            }
        }
        instruments.subscriptions.record(self.subscriptions, &[]);
        instruments.pending.record(self.pending, &[]);
        self.unpublished = 0;
    }
}

impl Drop for Batch {
    fn drop(&mut self) {
        self.flush();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    #[test]
    fn disabled_metrics_allocate_no_worker_batch() {
        assert!(WorkMetrics::default().batch().is_none());
        assert!(WorkMetrics::new(None).batch().is_none());
    }

    #[test]
    fn automatic_and_final_worker_flushes_preserve_counts_and_gauges() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("metrics.jsonl");
        let session = telemetry::Session::start(Some(&path)).unwrap();
        let metrics = WorkMetrics::new(session.meter());
        {
            let mut batch = metrics.batch().unwrap();
            batch.pending(7);
            for i in 0..6001 {
                batch.read(KeyFamily::Property, i % 2 == 0);
                batch.execution(EventKind::AssignProperty, i != 0);
            }
            batch.new_fact(KeyFamily::Property);
            batch.subscription(true);
            batch.subscription(false);
            batch.pending(0);
            // The tail is deliberately shorter than the automatic flush
            // threshold: dropping the worker must publish it too.
        }
        session.finish().unwrap();
        let records = std::fs::read_to_string(path).unwrap();
        let last: Value = serde_json::from_str(records.lines().last().unwrap()).unwrap();
        let metrics = last["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
            .as_array()
            .unwrap();
        let metric = |name: &str| metrics.iter().find(|m| m["name"] == name).unwrap();
        let sum = |name: &str| -> u64 {
            metric(name)["sum"]["dataPoints"]
                .as_array()
                .unwrap()
                .iter()
                .map(|point| point["asInt"].as_str().unwrap().parse::<u64>().unwrap())
                .sum()
        };
        assert_eq!(sum("debundle.inference.reads"), 6001);
        assert_eq!(sum("debundle.inference.absent_reads"), 3001);
        assert_eq!(sum("debundle.inference.evaluations"), 6001);
        let points = metric("debundle.inference.evaluations")["sum"]["dataPoints"]
            .as_array()
            .unwrap();
        assert_eq!(points.len(), 2);
        assert!(points.iter().any(|point| point["asInt"] == "1"));
        assert!(points.iter().any(|point| point["asInt"] == "6000"));
        assert_eq!(
            metric("debundle.inference.subscriptions")["gauge"]["dataPoints"][0]["asInt"],
            "1"
        );
        assert_eq!(
            metric("debundle.inference.pending")["gauge"]["dataPoints"][0]["asInt"],
            "0"
        );
        let rows = metric("debundle.inference.fact_rows")["gauge"]["dataPoints"]
            .as_array()
            .unwrap();
        assert_eq!(
            rows.iter()
                .map(|point| point["asInt"].as_str().unwrap().parse::<u64>().unwrap())
                .sum::<u64>(),
            1
        );
    }
}
