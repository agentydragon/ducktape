use serde::{Deserialize, Deserializer, Serialize, Serializer};
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

/// Hook types where Claude Code delivers `systemMessage` only to the UI
/// notification callback, not into the model conversation. Matches Python's
/// `_NON_REPL_HOOK_TYPES` in `server.py`. Used as a fallback for hooks that
/// aren't explicitly modeled (fall through to `Unknown`).
pub const NON_REPL_HOOK_NAMES: &[&str] = &[
    "SessionStart",
    "SessionEnd",
    "Setup",
    "CwdChanged",
    "FileChanged",
    "InstructionsLoaded",
    "WorktreeCreate",
    "WorktreeRemove",
    "ConfigChange",
];

#[derive(Debug)]
pub enum AnyHookInput {
    // --- REPL hooks (Claude Code injects systemMessage into the conversation) ---
    PreToolUse(PreToolUseInput),
    PostToolUse(PostToolUseInput),
    PostToolUseFailure(PostToolUseFailureInput),
    UserPromptSubmit(UserPromptSubmitInput),
    Stop(StopInput),
    SubagentStart(SubagentStartInput),
    SubagentStop(SubagentStopInput),
    Notification(NotificationInput),
    PermissionRequest(PermissionRequestInput),
    Elicitation(ElicitationInput),
    ElicitationResult(ElicitationResultInput),
    PreCompact(PreCompactInput),
    PostCompact(PostCompactInput),
    TeammateIdle(TeammateIdleInput),
    TaskCompleted(TaskCompletedInput),

    // --- Non-REPL hooks (systemMessage shown only in UI, not to model) ---
    SessionStart(SessionStartInput),
    WorktreeCreate(WorktreeCreateInput),
    SessionEnd(SessionEndInput),
    WorktreeRemove(WorktreeRemoveInput),
    Setup(SetupInput),
    CwdChanged(CwdChangedInput),
    FileChanged(FileChangedInput),
    InstructionsLoaded(InstructionsLoadedInput),
    ConfigChange(ConfigChangeInput),

    /// Hook type not explicitly modeled. Carries `hook_event_name` and
    /// `session_id` so `is_repl()` and `session_id()` still work for
    /// hooks added to Claude Code after this file was last updated.
    Unknown {
        hook_event_name: String,
        session_id: Option<String>,
    },
}

impl<'de> Deserialize<'de> for AnyHookInput {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = serde_json::Value::deserialize(deserializer)?;
        let event_name = raw
            .get("hook_event_name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_owned();
        macro_rules! parse {
            ($variant:path, $ty:ty) => {
                $variant(serde_json::from_value::<$ty>(raw).map_err(serde::de::Error::custom)?)
            };
        }
        Ok(match event_name.as_str() {
            "PreToolUse" => parse!(AnyHookInput::PreToolUse, PreToolUseInput),
            "PostToolUse" => parse!(AnyHookInput::PostToolUse, PostToolUseInput),
            "PostToolUseFailure" => {
                parse!(AnyHookInput::PostToolUseFailure, PostToolUseFailureInput)
            }
            "UserPromptSubmit" => parse!(AnyHookInput::UserPromptSubmit, UserPromptSubmitInput),
            "Stop" => parse!(AnyHookInput::Stop, StopInput),
            "SubagentStart" => parse!(AnyHookInput::SubagentStart, SubagentStartInput),
            "SubagentStop" => parse!(AnyHookInput::SubagentStop, SubagentStopInput),
            "Notification" => parse!(AnyHookInput::Notification, NotificationInput),
            "PermissionRequest" => parse!(AnyHookInput::PermissionRequest, PermissionRequestInput),
            "Elicitation" => parse!(AnyHookInput::Elicitation, ElicitationInput),
            "ElicitationResult" => parse!(AnyHookInput::ElicitationResult, ElicitationResultInput),
            "PreCompact" => parse!(AnyHookInput::PreCompact, PreCompactInput),
            "PostCompact" => parse!(AnyHookInput::PostCompact, PostCompactInput),
            "TeammateIdle" => parse!(AnyHookInput::TeammateIdle, TeammateIdleInput),
            "TaskCompleted" => parse!(AnyHookInput::TaskCompleted, TaskCompletedInput),
            "SessionStart" => parse!(AnyHookInput::SessionStart, SessionStartInput),
            "WorktreeCreate" => parse!(AnyHookInput::WorktreeCreate, WorktreeCreateInput),
            "SessionEnd" => parse!(AnyHookInput::SessionEnd, SessionEndInput),
            "WorktreeRemove" => parse!(AnyHookInput::WorktreeRemove, WorktreeRemoveInput),
            "Setup" => parse!(AnyHookInput::Setup, SetupInput),
            "CwdChanged" => parse!(AnyHookInput::CwdChanged, CwdChangedInput),
            "FileChanged" => parse!(AnyHookInput::FileChanged, FileChangedInput),
            "InstructionsLoaded" => {
                parse!(AnyHookInput::InstructionsLoaded, InstructionsLoadedInput)
            }
            "ConfigChange" => parse!(AnyHookInput::ConfigChange, ConfigChangeInput),
            _ => {
                let session_id = raw
                    .get("session_id")
                    .and_then(|v| v.as_str())
                    .map(str::to_owned);
                AnyHookInput::Unknown {
                    hook_event_name: event_name,
                    session_id,
                }
            }
        })
    }
}

impl Serialize for AnyHookInput {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        // Two-step via serde_json::Value so we can inject the hook_event_name
        // discriminator field that serde would normally handle via #[serde(tag)].
        macro_rules! tagged {
            ($inner:expr, $tag:literal) => {{
                let mut v = serde_json::to_value($inner).map_err(serde::ser::Error::custom)?;
                if let Some(obj) = v.as_object_mut() {
                    obj.insert("hook_event_name".to_owned(), $tag.into());
                }
                v.serialize(serializer)
            }};
        }
        match self {
            AnyHookInput::PreToolUse(h) => tagged!(h, "PreToolUse"),
            AnyHookInput::PostToolUse(h) => tagged!(h, "PostToolUse"),
            AnyHookInput::PostToolUseFailure(h) => tagged!(h, "PostToolUseFailure"),
            AnyHookInput::UserPromptSubmit(h) => tagged!(h, "UserPromptSubmit"),
            AnyHookInput::Stop(h) => tagged!(h, "Stop"),
            AnyHookInput::SubagentStart(h) => tagged!(h, "SubagentStart"),
            AnyHookInput::SubagentStop(h) => tagged!(h, "SubagentStop"),
            AnyHookInput::Notification(h) => tagged!(h, "Notification"),
            AnyHookInput::PermissionRequest(h) => tagged!(h, "PermissionRequest"),
            AnyHookInput::Elicitation(h) => tagged!(h, "Elicitation"),
            AnyHookInput::ElicitationResult(h) => tagged!(h, "ElicitationResult"),
            AnyHookInput::PreCompact(h) => tagged!(h, "PreCompact"),
            AnyHookInput::PostCompact(h) => tagged!(h, "PostCompact"),
            AnyHookInput::TeammateIdle(h) => tagged!(h, "TeammateIdle"),
            AnyHookInput::TaskCompleted(h) => tagged!(h, "TaskCompleted"),
            AnyHookInput::SessionStart(h) => tagged!(h, "SessionStart"),
            AnyHookInput::WorktreeCreate(h) => tagged!(h, "WorktreeCreate"),
            AnyHookInput::SessionEnd(h) => tagged!(h, "SessionEnd"),
            AnyHookInput::WorktreeRemove(h) => tagged!(h, "WorktreeRemove"),
            AnyHookInput::Setup(h) => tagged!(h, "Setup"),
            AnyHookInput::CwdChanged(h) => tagged!(h, "CwdChanged"),
            AnyHookInput::FileChanged(h) => tagged!(h, "FileChanged"),
            AnyHookInput::InstructionsLoaded(h) => tagged!(h, "InstructionsLoaded"),
            AnyHookInput::ConfigChange(h) => tagged!(h, "ConfigChange"),
            AnyHookInput::Unknown {
                hook_event_name,
                session_id,
            } => {
                let mut map = serde_json::Map::new();
                map.insert("hook_event_name".to_owned(), hook_event_name.clone().into());
                if let Some(sid) = session_id {
                    map.insert("session_id".to_owned(), sid.clone().into());
                }
                serde_json::Value::Object(map).serialize(serializer)
            }
        }
    }
}

impl AnyHookInput {
    /// Returns the session ID carried by this hook event, if any.
    pub fn session_id(&self) -> Option<String> {
        macro_rules! sid {
            ($h:expr) => {
                Some($h.base.session_id.clone())
            };
        }
        match self {
            AnyHookInput::SessionStart(h) => sid!(h),
            AnyHookInput::WorktreeCreate(h) => sid!(h),
            AnyHookInput::SessionEnd(h) => sid!(h),
            AnyHookInput::WorktreeRemove(h) => sid!(h),
            AnyHookInput::Setup(h) => sid!(h),
            AnyHookInput::CwdChanged(h) => sid!(h),
            AnyHookInput::FileChanged(h) => sid!(h),
            AnyHookInput::InstructionsLoaded(h) => sid!(h),
            AnyHookInput::ConfigChange(h) => sid!(h),
            AnyHookInput::PreToolUse(h) => sid!(h),
            AnyHookInput::PostToolUse(h) => sid!(h),
            AnyHookInput::PostToolUseFailure(h) => sid!(h),
            AnyHookInput::UserPromptSubmit(h) => sid!(h),
            AnyHookInput::Stop(h) => sid!(h),
            AnyHookInput::SubagentStart(h) => sid!(h),
            AnyHookInput::SubagentStop(h) => sid!(h),
            AnyHookInput::Notification(h) => sid!(h),
            AnyHookInput::PermissionRequest(h) => sid!(h),
            AnyHookInput::Elicitation(h) => sid!(h),
            AnyHookInput::ElicitationResult(h) => sid!(h),
            AnyHookInput::PreCompact(h) => sid!(h),
            AnyHookInput::PostCompact(h) => sid!(h),
            AnyHookInput::TeammateIdle(h) => sid!(h),
            AnyHookInput::TaskCompleted(h) => sid!(h),
            AnyHookInput::Unknown { session_id, .. } => session_id.clone(),
        }
    }

    /// Returns true if Claude Code injects `systemMessage` into the model
    /// conversation for this hook. Non-REPL hooks deliver it only to the UI
    /// notification callback; flushing the mailbox there has no effect.
    /// Matches `_NON_REPL_HOOK_TYPES` in `server.py`.
    pub fn is_repl(&self) -> bool {
        match self {
            AnyHookInput::SessionStart(_)
            | AnyHookInput::SessionEnd(_)
            | AnyHookInput::WorktreeCreate(_)
            | AnyHookInput::WorktreeRemove(_)
            | AnyHookInput::Setup(_)
            | AnyHookInput::CwdChanged(_)
            | AnyHookInput::FileChanged(_)
            | AnyHookInput::InstructionsLoaded(_)
            | AnyHookInput::ConfigChange(_) => false,
            AnyHookInput::Unknown {
                hook_event_name, ..
            } => !NON_REPL_HOOK_NAMES.contains(&hook_event_name.as_str()),
            _ => true,
        }
    }
}

// ---------------------------------------------------------------------------
// Shared base — present in every hook input
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, Serialize)]
pub struct HookInputBase {
    pub session_id: String,
    pub transcript_path: PathBuf,
    pub cwd: PathBuf,
    pub permission_mode: Option<String>,
}

// ---------------------------------------------------------------------------
// Non-REPL hook input structs
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, Serialize)]
pub struct SessionStartInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub source: String, // "startup" | "resume" | "clear" | "compact"
    pub model: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct WorktreeCreateInput {
    #[serde(flatten)]
    pub base: HookInputBase,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SessionEndInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub reason: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct WorktreeRemoveInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub worktree_path: PathBuf,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SetupInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub trigger: String, // "init" | "maintenance"
}

#[derive(Debug, Deserialize, Serialize)]
pub struct CwdChangedInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub old_cwd: String,
    pub new_cwd: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct FileChangedInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub file_path: String,
    pub event: String, // "change" | "add" | "unlink"
}

#[derive(Debug, Deserialize, Serialize)]
pub struct InstructionsLoadedInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub file_path: PathBuf,
    pub memory_type: String, // "User" | "Project" | "Local" | "Managed"
    pub load_reason: String,
    #[serde(default)]
    pub globs: Vec<String>,
    pub trigger_file_path: Option<PathBuf>,
    pub parent_file_path: Option<PathBuf>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ConfigChangeInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub source: String,
    pub file_path: Option<PathBuf>,
}

// ---------------------------------------------------------------------------
// REPL hook input structs
// ---------------------------------------------------------------------------

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
pub struct PostToolUseFailureInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub tool_name: String,
    pub tool_input: serde_json::Value,
    pub tool_use_id: String,
    pub error: String,
    pub is_interrupt: Option<bool>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct UserPromptSubmitInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub prompt: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct StopInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub stop_hook_active: bool,
    pub last_assistant_message: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SubagentStartInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub agent_id: String,
    pub agent_type: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SubagentStopInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub stop_hook_active: bool,
    pub agent_id: String,
    pub agent_type: String,
    pub agent_transcript_path: String,
    pub last_assistant_message: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct NotificationInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub message: String,
    pub title: Option<String>,
    pub notification_type: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct PermissionRequestInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub tool_name: String,
    pub tool_input: serde_json::Value,
    #[serde(default)]
    pub permission_suggestions: Vec<serde_json::Value>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ElicitationInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub mcp_server_name: String,
    pub message: String,
    pub mode: Option<String>,
    pub url: Option<String>,
    pub elicitation_id: Option<String>,
    pub requested_schema: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ElicitationResultInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub mcp_server_name: String,
    pub action: String, // "accept" | "deny" | "cancel"
    pub content: Option<serde_json::Value>,
    pub mode: Option<String>,
    pub elicitation_id: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct PreCompactInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub trigger: String, // "manual" | "auto"
    pub custom_instructions: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct PostCompactInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub trigger: String,
    pub compact_summary: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct TeammateIdleInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub teammate_name: String,
    pub team_name: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct TaskCompletedInput {
    #[serde(flatten)]
    pub base: HookInputBase,
    pub task_id: String,
    pub task_subject: String,
    pub task_description: Option<String>,
    pub teammate_name: Option<String>,
    pub team_name: Option<String>,
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
        assert!(
            matches!(&input, AnyHookInput::Unknown { hook_event_name, session_id }
                if hook_event_name == "SomeNewHook" && session_id.as_deref() == Some("x"))
        );
    }

    #[test]
    fn user_prompt_submit_deserializes() {
        let json = r#"{
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sess-123",
            "transcript_path": "/tmp/t.json",
            "cwd": "/",
            "prompt": "hello world"
        }"#;
        let input: AnyHookInput = serde_json::from_str(json).unwrap();
        match input {
            AnyHookInput::UserPromptSubmit(u) => {
                assert_eq!(u.base.session_id, "sess-123");
                assert_eq!(u.prompt, "hello world");
            }
            _ => panic!("expected UserPromptSubmit"),
        }
    }

    #[test]
    fn stop_deserializes() {
        let json = r#"{
            "hook_event_name": "Stop",
            "session_id": "sess-456",
            "transcript_path": "/tmp/t.json",
            "cwd": "/",
            "stop_hook_active": false,
            "last_assistant_message": "Done."
        }"#;
        let input: AnyHookInput = serde_json::from_str(json).unwrap();
        match input {
            AnyHookInput::Stop(s) => {
                assert_eq!(s.base.session_id, "sess-456");
                assert!(!s.stop_hook_active);
                assert_eq!(s.last_assistant_message.as_deref(), Some("Done."));
            }
            _ => panic!("expected Stop"),
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
    fn hook_request_deserializes() {
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
        assert!(matches!(req.hook, AnyHookInput::PreToolUse(_)));
        assert_eq!(req.env.get("HOME").map(String::as_str), Some("/root"));
    }
}
