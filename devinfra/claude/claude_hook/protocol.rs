use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Body of `POST /hook`: Claude Code's hook JSON verbatim as `hook`, and the
/// `claude-hook` client's environment as `env`.
#[derive(Debug, Deserialize)]
pub struct HookRequest {
    pub hook: HookInput,
    pub env: HashMap<String, String>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct HookResponse {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output: Option<HookOutput>,
}

/// Hook types where Claude Code delivers `systemMessage` only to the UI
/// notification callback, not into the model conversation. Every other hook is
/// a REPL hook.
///
/// WorktreeCreate / WorktreeRemove are intentionally excluded: the daemon
/// does not implement worktree management (Claude Code expects the hook to
/// run `git worktree add` and print the path on stdout — a plain-text
/// contract incompatible with the daemon's JSON-in/JSON-out architecture).
/// Not registering these hooks in settings.json avoids the no-op stub that
/// previously caused "returned a path that is not a directory: {}" errors.
pub const NON_REPL_HOOK_NAMES: &[&str] = &[
    "SessionStart",
    "SessionEnd",
    "Setup",
    "CwdChanged",
    "FileChanged",
    "InstructionsLoaded",
    "ConfigChange",
];

/// Only the input fields the daemon reads. Claude Code's full input contract is modeled in
/// `devinfra/claude/claude_api/hooks/`.
#[derive(Debug, Deserialize)]
pub struct HookInput {
    pub hook_event_name: String,
    pub session_id: String,
}

impl HookInput {
    /// Returns true if Claude Code injects `systemMessage` into the model
    /// conversation for this hook. Non-REPL hooks deliver it only to the UI
    /// notification callback; flushing the mailbox there has no effect.
    pub fn is_repl(&self) -> bool {
        !NON_REPL_HOOK_NAMES.contains(&self.hook_event_name.as_str())
    }
}

/// Only the output fields the daemon sets. Claude Code's full output contract is modeled in
/// `devinfra/claude/claude_api/hooks/output.py`.
#[derive(Debug, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct HookOutput {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub system_message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hook_specific_output: Option<AnyHookSpecificOutput>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "hookEventName")]
pub enum AnyHookSpecificOutput {
    SessionStart(SessionStartSpecificOutput),
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionStartSpecificOutput {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub additional_context: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hook_request_reads_event_name_and_session_id_and_ignores_the_rest() {
        let json = r#"{
            "hook": {
                "hook_event_name": "PreToolUse",
                "session_id": "req-sess",
                "transcript_path": "/tmp/t.json",
                "cwd": "/project",
                "tool_name": "Bash",
                "tool_input": {},
                "tool_use_id": "tu_001"
            },
            "env": {
                "PATH": "/usr/bin",
                "HOME": "/root"
            }
        }"#;
        let req: HookRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.hook.hook_event_name, "PreToolUse");
        assert_eq!(req.hook.session_id, "req-sess");
        assert_eq!(req.env.get("HOME").map(String::as_str), Some("/root"));
    }

    #[test]
    fn is_repl_excludes_only_the_listed_non_repl_hooks() {
        for (name, is_repl) in [
            ("SessionStart", false),
            ("ConfigChange", false),
            ("PreToolUse", true),
            ("Stop", true),
            ("SomeNewHook", true),
        ] {
            let hook = HookInput {
                hook_event_name: name.into(),
                session_id: "s".into(),
            };
            assert_eq!(hook.is_repl(), is_repl, "{name}");
        }
    }

    #[test]
    fn serialize_hook_output_omits_none() {
        let output = HookOutput::default();
        let json = serde_json::to_string(&output).unwrap();
        assert_eq!(json, "{}");
    }

    #[test]
    fn serialize_hook_output_with_session_start() {
        let output = HookOutput {
            hook_specific_output: Some(AnyHookSpecificOutput::SessionStart(
                SessionStartSpecificOutput {
                    additional_context: Some("hello".into()),
                },
            )),
            ..Default::default()
        };
        let json = serde_json::to_string(&output).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["hookSpecificOutput"]["hookEventName"], "SessionStart");
        assert_eq!(v["hookSpecificOutput"]["additionalContext"], "hello");
    }

    #[test]
    fn serialize_hook_response_empty() {
        let resp = HookResponse { output: None };
        let json = serde_json::to_string(&resp).unwrap();
        assert_eq!(json, "{}");
    }
}
