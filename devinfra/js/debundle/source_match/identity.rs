use super::*;

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
