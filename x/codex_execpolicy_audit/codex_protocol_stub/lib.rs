//! Local stub of `codex_protocol::parse_command::ParsedCommand`.
//!
//! The real `codex-protocol` crate is a heavy (~21k-line) closure. `codex-shell-command`
//! references only this one type, so we stub it here under the extern crate name
//! `codex_protocol` (see `BUILD.bazel` `crate_name`) and skip the heavy dep entirely.
//! Shape copied from openai/codex `codex-rs/protocol/src/parse_command.rs` (Apache-2.0).

pub mod parse_command {
    use std::path::PathBuf;

    #[derive(Debug, Clone, PartialEq, Eq)]
    pub enum ParsedCommand {
        Read {
            cmd: String,
            name: String,
            path: PathBuf,
        },
        ListFiles {
            cmd: String,
            path: Option<String>,
        },
        Search {
            cmd: String,
            query: Option<String>,
            path: Option<String>,
        },
        Unknown {
            cmd: String,
        },
    }
}
