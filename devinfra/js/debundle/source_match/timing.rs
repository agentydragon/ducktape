use super::*;

pub(crate) const SOURCE_MATCH_TIMINGS_ENV: &str = "DUCKTAPE_SOURCE_MATCH_TIMINGS";

pub(crate) const SOURCE_MATCH_TIMING_THRESHOLD_ENV: &str =
    "DUCKTAPE_SOURCE_MATCH_TIMING_THRESHOLD_MS";

pub(crate) const SOURCE_MATCH_TIMING_PREVIEW_ENV: &str = "DUCKTAPE_SOURCE_MATCH_TIMING_PREVIEW";

#[derive(Debug)]
pub(crate) struct SourceMatchTimingConfig {
    threshold: Duration,
    include_preview: bool,
}

pub(crate) fn source_match_timing_config() -> Option<&'static SourceMatchTimingConfig> {
    static CONFIG: OnceLock<Option<SourceMatchTimingConfig>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            let enabled = std::env::var(SOURCE_MATCH_TIMINGS_ENV).ok()?;
            if matches!(
                enabled.to_ascii_lowercase().as_str(),
                "" | "0" | "false" | "off" | "no"
            ) {
                return None;
            }
            let threshold_ms = std::env::var(SOURCE_MATCH_TIMING_THRESHOLD_ENV)
                .ok()
                .and_then(|raw| raw.parse::<u64>().ok())
                .unwrap_or(0);
            let include_preview = std::env::var(SOURCE_MATCH_TIMING_PREVIEW_ENV)
                .ok()
                .is_none_or(|raw| {
                    !matches!(
                        raw.to_ascii_lowercase().as_str(),
                        "" | "0" | "false" | "off" | "no"
                    )
                });
            Some(SourceMatchTimingConfig {
                threshold: Duration::from_millis(threshold_ms),
                include_preview,
            })
        })
        .as_ref()
}

pub(crate) fn trace_source_match<T>(
    kind: &str,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    run: impl FnOnce() -> Result<T>,
    summarize: impl FnOnce(&T) -> String,
) -> Result<T> {
    let Some(config) = source_match_timing_config() else {
        return run();
    };
    let started = Instant::now();
    let result = run();
    let elapsed = started.elapsed();
    if elapsed >= config.threshold {
        let status = match &result {
            Ok(value) => summarize(value),
            Err(error) => format!("error={}", first_error_line(error)),
        };
        eprintln!(
            "[debundle source_match] elapsed_ms={} request={} kind={} {} {}",
            elapsed.as_millis(),
            request_id,
            kind,
            status,
            source_match_timing_selector_details(selector, config),
        );
    }
    result
}

pub fn source_match_preview(source: &str) -> String {
    let collapsed = source.split_whitespace().collect::<Vec<_>>().join(" ");
    truncate_for_log(&collapsed, 180)
}

pub(crate) fn source_match_timing_selector_details(
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
    if let Some(target_statement) = selector.target_statement {
        fields.push(format!("target_statement={target_statement}"));
    }
    if let Some(target_statements) = &selector.target_statements {
        fields.push(format!("target_statements={target_statements:?}"));
    }
    if config.include_preview {
        fields.push(format!(
            "selector={}",
            source_match_preview(&selector.match_source)
        ));
    }
    fields.join(" ")
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
    state.update(b"\0");
    if let Some(target_statement) = selector.target_statement {
        state.update(format!("target_statement={target_statement}").as_bytes());
    }
    state.update(b"\0");
    if let Some(target_statements) = &selector.target_statements {
        state.update(format!("target_statements={target_statements:?}").as_bytes());
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

pub(crate) fn render_timing_names<'a>(names: impl Iterator<Item = &'a str>) -> String {
    const MAX_NAMES: usize = 6;
    let names = names.collect::<Vec<_>>();
    if names.is_empty() {
        return "<none>".to_string();
    }
    let mut rendered = names
        .iter()
        .take(MAX_NAMES)
        .map(|name| format!("`{name}`"))
        .collect::<Vec<_>>();
    if names.len() > MAX_NAMES {
        rendered.push(format!("+{} more", names.len() - MAX_NAMES));
    }
    rendered.join(",")
}

pub(crate) fn render_timing_groups(groups: &[Vec<usize>]) -> String {
    const MAX_GROUPS: usize = 4;
    if groups.is_empty() {
        return "[]".to_string();
    }
    let mut rendered = groups
        .iter()
        .take(MAX_GROUPS)
        .map(|group| format!("{group:?}"))
        .collect::<Vec<_>>();
    if groups.len() > MAX_GROUPS {
        rendered.push(format!("+{} more", groups.len() - MAX_GROUPS));
    }
    rendered.join(",")
}

pub(crate) fn render_timing_body_indices<'a>(indices: impl Iterator<Item = &'a usize>) -> String {
    const MAX_INDICES: usize = 8;
    let indices = indices.copied().collect::<Vec<_>>();
    if indices.is_empty() {
        return "[]".to_string();
    }
    let mut rendered = indices
        .iter()
        .take(MAX_INDICES)
        .map(usize::to_string)
        .collect::<Vec<_>>();
    if indices.len() > MAX_INDICES {
        rendered.push(format!("+{} more", indices.len() - MAX_INDICES));
    }
    format!("[{}]", rendered.join(","))
}
