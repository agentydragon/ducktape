use std::sync::OnceLock;
use std::time::{Duration, Instant};

use super::*;

pub(crate) const SOURCE_MATCH_TIMINGS_ENV: &str = "DUCKTAPE_SOURCE_MATCH_TIMINGS";

pub(crate) const SOURCE_MATCH_TIMING_THRESHOLD_ENV: &str =
    "DUCKTAPE_SOURCE_MATCH_TIMING_THRESHOLD_MS";

pub(crate) const SOURCE_MATCH_TIMING_PREVIEW_ENV: &str = "DUCKTAPE_SOURCE_MATCH_TIMING_PREVIEW";

struct SourceMatchTimingConfig {
    threshold: Duration,
    include_preview: bool,
}

fn env_disabled(raw: &str) -> bool {
    matches!(
        raw.to_ascii_lowercase().as_str(),
        "" | "0" | "false" | "off" | "no"
    )
}

fn source_match_timing_config() -> Option<&'static SourceMatchTimingConfig> {
    static CONFIG: OnceLock<Option<SourceMatchTimingConfig>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            let enabled = std::env::var(SOURCE_MATCH_TIMINGS_ENV).ok()?;
            if env_disabled(&enabled) {
                return None;
            }
            let threshold_ms = std::env::var(SOURCE_MATCH_TIMING_THRESHOLD_ENV)
                .ok()
                .and_then(|raw| raw.parse::<u64>().ok())
                .unwrap_or(0);
            let include_preview = std::env::var(SOURCE_MATCH_TIMING_PREVIEW_ENV)
                .ok()
                .is_none_or(|raw| !env_disabled(&raw));
            Some(SourceMatchTimingConfig {
                threshold: Duration::from_millis(threshold_ms),
                include_preview,
            })
        })
        .as_ref()
}

/// True iff `DUCKTAPE_SOURCE_MATCH_TIMINGS` is set to an enabled value, so a
/// caller can skip computing the per-emit `status`/elapsed when timing is off.
pub(crate) fn source_match_timings_enabled() -> bool {
    source_match_timing_config().is_some()
}

/// Emit one `[debundle source_match]` timing line for a resolved selector, when
/// timing is enabled and the elapsed duration meets the threshold. `kind` names
/// the selector surface (e.g. `members[].selector.source_match export=`foo``) and
/// `status` carries the per-surface result fields (e.g.
/// `body_indices=[0] binding=foo`). The selector identity keys and (optionally)
/// the source preview are appended uniformly. Emitting from the resolver's
/// per-selector path (behind the chunk-level resolution cache) means a selector
/// reused across modules is timed once per chunk.
pub(crate) fn emit_source_match_timing(
    kind: &str,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    elapsed: Duration,
    status: &str,
) {
    let Some(config) = source_match_timing_config() else {
        return;
    };
    if elapsed < config.threshold {
        return;
    }
    eprintln!(
        "[debundle source_match] elapsed_ms={} request={} kind={} {} {}",
        elapsed.as_millis(),
        request_id,
        kind,
        status,
        source_match_timing_selector_details(selector, config),
    );
}

/// Run `f`, returning its result paired with the wall-clock duration — the
/// minimal timer the resolver wraps a per-selector resolution in. Always runs
/// `f` (the elapsed measurement is cheap); the emit decision is `emit_*`'s.
pub(crate) fn time_source_match<T>(f: impl FnOnce() -> T) -> (T, Duration) {
    let started = Instant::now();
    let value = f();
    (value, started.elapsed())
}

fn source_match_timing_selector_details(
    selector: &AnonymousStatementSelector,
    config: &SourceMatchTimingConfig,
) -> String {
    let mut fields = vec![
        format!("selector_key={}", selector_key(selector)),
        format!("body_key={}", selector_body_key(selector)),
    ];
    if let Some(target_binding) = selector.target_binding.as_deref() {
        fields.push(format!("target_binding=`{target_binding}`"));
    }
    if config.include_preview {
        fields.push(format!(
            "selector={}",
            source_match_preview(&selector.match_source)
        ));
    }
    fields.join(" ")
}

pub fn source_match_preview(source: &str) -> String {
    let collapsed = source.split_whitespace().collect::<Vec<_>>().join(" ");
    truncate_for_log(&collapsed, 180)
}

pub fn selector_body_key(selector: &AnonymousStatementSelector) -> String {
    let mut state = Fnv1a64::new();
    state.update(b"body");
    state.update(format!("{:?}", selector.identifiers).as_bytes());
    state.update(b"\0");
    state.update(normalized_selector_source(&selector.match_source).as_bytes());
    format!("{:016x}", state.finish())
}

pub fn selector_key(selector: &AnonymousStatementSelector) -> String {
    let mut state = Fnv1a64::new();
    state.update(b"selector");
    state.update(format!("{:?}", selector.identifiers).as_bytes());
    state.update(b"\0");
    state.update(normalized_selector_source(&selector.match_source).as_bytes());
    state.update(b"\0");
    if let Some(target_binding) = selector.target_binding.as_deref() {
        state.update(b"target_binding=");
        state.update(target_binding.as_bytes());
    }
    format!("{:016x}", state.finish())
}

pub(crate) fn normalized_selector_source(source: &str) -> String {
    source.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub(crate) struct Fnv1a64 {
    value: u64,
}

impl Fnv1a64 {
    const OFFSET: u64 = 0xcbf29ce484222325;
    const PRIME: u64 = 0x100000001b3;

    fn new() -> Self {
        Self {
            value: Self::OFFSET,
        }
    }

    fn update(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.value ^= u64::from(*byte);
            self.value = self.value.wrapping_mul(Self::PRIME);
        }
    }

    fn finish(&self) -> u64 {
        self.value
    }
}

pub(crate) fn truncate_for_log(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        return value.to_string();
    }
    let mut truncated = value.chars().take(max_chars).collect::<String>();
    truncated.push_str("...");
    truncated
}
