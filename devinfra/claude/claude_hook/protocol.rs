use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;

// ---------------------------------------------------------------------------
// Hook request / response envelope (daemon's /hook HTTP endpoint)
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, Serialize)]
pub struct HookRequest {
    pub hook: AnyHookInput,
    pub env: HashMap<String, String>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct HookResponse {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output: Option<HookOutput>,
}

// ---------------------------------------------------------------------------
// Hook inputs — discriminated union on `hook_event_name` (PascalCase values)
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "hook_event_name")]
pub enum AnyHookInput {
    SessionStart(SessionStartInput),
    PreToolUse(PreToolUseInput),
    PostToolUse(PostToolUseInput),
    WorktreeCreate(WorktreeCreateInput),
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct HookInputBase {
    pub session_id: String,
    pub transcript_path: PathBuf,
    pub cwd: PathBuf,
    pub permission_mode: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SessionStartInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub source: String, // "startup" | "resume" | "clear" | "compact"
    pub model: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct PreToolUseInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub tool_name: String,
    pub tool_input: serde_json::Value,
    pub tool_use_id: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct PostToolUseInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub tool_name: String,
    pub tool_input: serde_json::Value,
    pub tool_use_id: String,
    pub tool_response: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct WorktreeCreateInput {
    #[serde(flatten)]
    pub base: HookInputBase,
}

// ---------------------------------------------------------------------------
// Hook output (camelCase, skip None fields)
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct HookOutput {
    #[serde(rename = "continue", skip_serializing_if = "Option::is_none")]
    pub continue_: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub suppress_output: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stop_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub decision: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub system_message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hook_specific_output: Option<AnyHookSpecificOutput>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "hookEventName")]
pub enum AnyHookSpecificOutput {
    SessionStart(SessionStartSpecificOutput),
    PreToolUse(PreToolUseSpecificOutput),
    PostToolUse(PostToolUseSpecificOutput),
}

#[derive(Debug, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct SessionStartSpecificOutput {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub additional_context: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub initial_user_message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub watch_paths: Option<Vec<String>>,
}

#[derive(Debug, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct PreToolUseSpecificOutput {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub permission_decision: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub permission_decision_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub additional_context: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct PostToolUseSpecificOutput {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub additional_context: Option<String>,
}

// ---------------------------------------------------------------------------
// Shim exec RPC (/shim-exec endpoint) — snake_case on the wire
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, Serialize)]
pub struct ShimExecRequest {
    pub shim: String,
    pub session_id: String,
    pub cwd: PathBuf,
    pub argv: Vec<String>,
    pub pid: u32,
    pub env: HashMap<String, String>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum ShimResponse {
    #[serde(rename = "blocked")]
    Blocked { message: String },
    #[serde(rename = "execve")]
    Execve { argv: Vec<String> },
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deserialize_session_start_input() {
        let json = r#"{
            "hook_event_name": "SessionStart",
            "session_id": "test-123",
            "transcript_path": "/tmp/transcript.json",
            "cwd": "/project",
            "permission_mode": "default",
            "source": "startup",
            "model": "claude-sonnet-4-6"
        }"#;
        let input: AnyHookInput = serde_json::from_str(json).unwrap();
        match input {
            AnyHookInput::SessionStart(s) => {
                assert_eq!(s.base.session_id, "test-123");
                assert_eq!(s.source, "startup");
                assert_eq!(s.model.as_deref(), Some("claude-sonnet-4-6"));
            }
            _ => panic!("expected SessionStart"),
        }
    }

    #[test]
    fn deserialize_pre_tool_use_input() {
        let json = r#"{
            "hook_event_name": "PreToolUse",
            "session_id": "test-456",
            "transcript_path": "/tmp/t.json",
            "cwd": "/project",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_use_id": "tool_001"
        }"#;
        let input: AnyHookInput = serde_json::from_str(json).unwrap();
        match input {
            AnyHookInput::PreToolUse(p) => {
                assert_eq!(p.tool_name, "Bash");
                assert_eq!(p.tool_use_id, "tool_001");
            }
            _ => panic!("expected PreToolUse"),
        }
    }

    #[test]
    fn unknown_hook_deserializes() {
        let json = r#"{
            "hook_event_name": "SomeNewHook",
            "session_id": "x",
            "transcript_path": "/tmp/t.json",
            "cwd": "/"
        }"#;
        let input: AnyHookInput = serde_json::from_str(json).unwrap();
        assert!(matches!(input, AnyHookInput::Unknown));
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
                    ..Default::default()
                },
            )),
            ..Default::default()
        };
        let json = serde_json::to_string(&output).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["hookSpecificOutput"]["hookEventName"], "SessionStart");
        assert_eq!(v["hookSpecificOutput"]["additionalContext"], "hello");
        // None fields must not appear
        assert!(v.get("continue").is_none());
        assert!(v.get("decision").is_none());
    }

    #[test]
    fn serialize_hook_response_empty() {
        let resp = HookResponse { output: None };
        let json = serde_json::to_string(&resp).unwrap();
        assert_eq!(json, "{}");
    }

    #[test]
    fn serialize_shim_response_blocked() {
        let resp = ShimResponse::Blocked {
            message: "nope".into(),
        };
        let json = serde_json::to_string(&resp).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["kind"], "blocked");
        assert_eq!(v["message"], "nope");
    }

    #[test]
    fn serialize_shim_response_execve() {
        let resp = ShimResponse::Execve {
            argv: vec!["git".into(), "status".into()],
        };
        let json = serde_json::to_string(&resp).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["kind"], "execve");
        assert_eq!(v["argv"], serde_json::json!(["git", "status"]));
    }

    #[test]
    fn deserialize_shim_exec_request() {
        let json = r#"{
            "shim": "bazelisk",
            "session_id": "test-789",
            "cwd": "/project",
            "argv": ["bazelisk", "build", "//..."],
            "pid": 12345,
            "env": {"PATH": "/usr/bin"}
        }"#;
        let req: ShimExecRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.shim, "bazelisk");
        assert_eq!(req.pid, 12345);
        assert_eq!(req.argv.len(), 3);
    }

    #[test]
    fn deserialize_hook_request_envelope() {
        let json = r#"{
            "hook": {
                "hook_event_name": "SessionStart",
                "session_id": "s1",
                "transcript_path": "/tmp/t",
                "cwd": "/p",
                "source": "startup"
            },
            "env": {"HOME": "/root"}
        }"#;
        let req: HookRequest = serde_json::from_str(json).unwrap();
        assert!(matches!(req.hook, AnyHookInput::SessionStart(_)));
        assert_eq!(req.env.get("HOME").unwrap(), "/root");
    }
}
