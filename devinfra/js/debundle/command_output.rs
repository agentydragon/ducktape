//! Command output formatting and stdout emission.

use anyhow::Result;
use peel::OutputFormat;
use serde::Serialize;

/// Emit `value` in the requested `format`. `text_render` produces the
/// human-readable rendering when `format` resolves to text.
pub fn print_report<T, F>(value: &T, format: OutputFormat, text_render: F) -> Result<()>
where
    T: Serialize,
    F: FnOnce(&T, &mut String),
{
    match format {
        OutputFormat::Text => {
            let mut buf = String::new();
            text_render(value, &mut buf);
            if !buf.is_empty() && !buf.ends_with('\n') {
                buf.push('\n');
            }
            print!("{buf}");
            Ok(())
        }
        OutputFormat::Json => {
            println!("{}", serde_json::to_string_pretty(value)?);
            Ok(())
        }
        OutputFormat::Ndjson => {
            // For ndjson on a non-array shape, fall back to one
            // compact JSON document per call. Commands whose primary
            // output is an array (atoms, scc, modules list) override
            // and emit one line per item.
            println!("{}", serde_json::to_string(value)?);
            Ok(())
        }
    }
}

/// For a command whose primary output is an array: when `format` is `Ndjson`,
/// emit one compact JSON line per item and return `true` (the caller should
/// stop). Otherwise emit nothing and return `false`, so the caller falls through
/// to [`print_report`] for the text/json rendering of the whole report.
pub fn print_ndjson_list<T: Serialize>(items: &[T], format: OutputFormat) -> Result<bool> {
    if format != OutputFormat::Ndjson {
        return Ok(false);
    }
    for item in items {
        println!("{}", serde_json::to_string(item)?);
    }
    Ok(true)
}
