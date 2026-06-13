use crate::types::{Endpoint, MetadataKey, MetadataRequest, ReplayKey};

pub fn parse_metadata_request(path: &str, query: Option<&str>) -> Option<MetadataRequest> {
    let endpoint = match path {
        "/wayback/available" => Endpoint::Availability,
        "/cdx/search/cdx" => Endpoint::Cdx,
        _ => return None,
    };
    let raw_query = query.unwrap_or_default().to_string();
    Some(MetadataRequest {
        key: MetadataKey {
            endpoint,
            normalized_query: normalize_query(query.unwrap_or_default()),
        },
        raw_query,
    })
}

fn normalize_query(query: &str) -> String {
    let mut parts = query
        .split('&')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    parts.sort_unstable();
    parts.join("&")
}

pub fn parse_replay_path(path: &str) -> Option<ReplayKey> {
    let rest = path.strip_prefix("/web/")?;
    let slash = rest.find('/')?;
    let ts_modifier = &rest[..slash];
    let original_url = &rest[slash + 1..];
    if original_url.is_empty() {
        return None;
    }
    let digit_count = ts_modifier.chars().take_while(char::is_ascii_digit).count();
    if !(4..=14).contains(&digit_count) {
        return None;
    }
    let capture_ts = &ts_modifier[..digit_count];
    let modifier = &ts_modifier[digit_count..];
    if !(modifier.is_empty() || (modifier.len() == 3 && modifier.ends_with('_'))) {
        return None;
    }
    Some(ReplayKey {
        capture_ts: capture_ts.to_string(),
        modifier: modifier.to_string(),
        original_url: original_url.to_string(),
    })
}
