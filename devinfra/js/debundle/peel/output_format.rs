use std::io::IsTerminal;

use clap::ValueEnum;

/// Uniform output format selector for every read-only query command,
/// per docs/cli.md § "Output format". `text` is the default when
/// stdout is a tty; `json` is the default when stdout is piped.
/// `ndjson` emits one JSON value per line so streaming consumers
/// (`jq -c`, downstream pipes) don't have to buffer.
#[derive(Debug, Clone, Copy, ValueEnum, PartialEq, Eq)]
#[value(rename_all = "snake_case")]
pub enum OutputFormat {
    Text,
    Json,
    Ndjson,
}

impl OutputFormat {
    /// Resolve a user-supplied `--format` against the tty default.
    pub fn resolve(opt: Option<Self>) -> Self {
        if let Some(f) = opt {
            return f;
        }
        if std::io::stdout().is_terminal() {
            Self::Text
        } else {
            Self::Json
        }
    }
}
