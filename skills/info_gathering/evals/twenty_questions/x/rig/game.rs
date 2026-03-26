//! Core game loop for Twenty Questions using Rig.
//!
//! The guesser agent has `tool_choice=Required` and three tools:
//! `ask_yes_no_question`, `guess_answer`, and `exec` (scratch computation).
//!
//! The game tools (`ask_yes_no_question`, `guess_answer`) internally invoke the
//! simulator (a single LLM call with `answer`/`correct_answer`/`invalid_input`
//! tools) and return the result string to the guesser.
//!
//! The guesser also has access to a scratch container exec tool, backed by
//! a Docker container created before the game starts and cleaned up after.

use crate::docker_exec::ScratchContainer;
use chrono::Utc;
use rig::client::{CompletionClient, ProviderClient};
use rig::completion::{Chat, Completion, CompletionModel, CompletionResponse, ToolDefinition};
use rig::message::{AssistantContent, Message, ToolChoice};
use rig::tool::Tool;
use runfiles::{Runfiles, rlocation};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::fmt;
use std::fs;
use std::io::Write;
use std::path::Path;
use std::sync::{Arc, Mutex};

// ---------------------------------------------------------------------------
// Prompt loading from shared text files via Bazel runfiles
// ---------------------------------------------------------------------------

const PROMPTS_DIR: &str = "_main/skills/info_gathering/evals/twenty_questions";

fn load_runfile(r: &Runfiles, rlocation_path: &str) -> String {
    let path = rlocation!(r, rlocation_path)
        .unwrap_or_else(|| panic!("Could not resolve runfile: {rlocation_path}"));
    fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("Could not read {}: {e}", path.display()))
        .trim()
        .to_string()
}

fn load_sim_prompt(r: &Runfiles, turn_limit: u32, secret: &str) -> String {
    let template = load_runfile(r, &format!("{PROMPTS_DIR}/sim.txt"));
    template
        .replace("{turn_limit}", &turn_limit.to_string())
        .replace("{secret}", secret)
}

fn load_first_user_message(r: &Runfiles, domain_description: &str, turn_limit: u32) -> String {
    let template = load_runfile(r, &format!("{PROMPTS_DIR}/first_user_message.txt"));
    template
        .replace("{domain_description}", domain_description)
        .replace("{turn_limit}", &turn_limit.to_string())
}

fn load_scratch_system_note(r: &Runfiles) -> String {
    load_runfile(r, &format!("{PROMPTS_DIR}/scratch_system_note.txt"))
}

// ---------------------------------------------------------------------------
// Game variant configuration
// ---------------------------------------------------------------------------

pub struct Variant {
    pub name: &'static str,
    pub domain_description: &'static str,
    pub secret: &'static str,
    pub turn_limit: u32,
}

pub fn get_variant(name: &str) -> anyhow::Result<Variant> {
    match name {
        "states" => Ok(Variant {
            name: "states",
            domain_description: "a US state",
            secret: "New Mexico",
            turn_limit: 20,
        }),
        "wide" => Ok(Variant {
            name: "wide",
            domain_description: "a thing — could be anything: object, place, concept, activity, anything",
            secret: "a sourdough starter",
            turn_limit: 25,
        }),
        _ => anyhow::bail!("Unknown variant: {name}. Choose 'states' or 'wide'."),
    }
}

// ---------------------------------------------------------------------------
// Logging / summary types
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Serialize, Deserialize)]
pub enum Player {
    #[serde(rename = "guesser")]
    Guesser,
    #[serde(rename = "simulator")]
    Simulator,
}

#[derive(Serialize, Deserialize)]
pub struct LogEntry {
    pub timestamp: chrono::DateTime<chrono::Utc>,
    pub player: Player,
    pub content: String,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum GameResult {
    #[serde(rename = "correct")]
    Correct { turns: u32 },
    #[serde(rename = "timeout")]
    Timeout { limit: u32 },
}

#[derive(Serialize, Deserialize)]
pub struct RunSummary {
    pub eval_name: String,
    pub framework: String,
    pub model: String,
    pub api: String,
    pub turns: u32,
    pub invalid_input_count: u32,
    pub result: GameResult,
}

// ---------------------------------------------------------------------------
// Shared game state
// ---------------------------------------------------------------------------

/// Mutable game state shared between guesser tools and the game loop.
struct GameState {
    turns: u32,
    turn_limit: u32,
    invalid_input_count: u32,
    game_over: bool,
    result: GameResult,
    sim_history: Vec<Message>,
    log_entries: Vec<LogEntry>,
}

impl GameState {
    fn new(turn_limit: u32) -> Self {
        Self {
            turns: 0,
            turn_limit,
            invalid_input_count: 0,
            game_over: false,
            result: GameResult::Timeout { limit: turn_limit },
            sim_history: Vec::new(),
            log_entries: Vec::new(),
        }
    }
}

type SharedGameState = Arc<Mutex<GameState>>;

// ---------------------------------------------------------------------------
// Simulator tool types and single-turn invocation
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
enum SimAction {
    Answer(String),
    CorrectAnswer,
    InvalidInput(String),
}

type SharedAction = Arc<Mutex<Option<SimAction>>>;

#[derive(Debug)]
struct ToolCallError(String);

impl fmt::Display for ToolCallError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "tool call error: {}", self.0)
    }
}

impl std::error::Error for ToolCallError {}

// -- simulator answer tool --

#[derive(Deserialize)]
struct AnswerArgs {
    response: String,
}

struct SimAnswerTool {
    action: SharedAction,
}

impl Tool for SimAnswerTool {
    const NAME: &'static str = "answer";
    type Error = ToolCallError;
    type Args = AnswerArgs;
    type Output = String;

    async fn definition(&self, _prompt: String) -> ToolDefinition {
        ToolDefinition {
            name: "answer".to_string(),
            description: "Answer the player's yes/no question with yes, no, or sort_of."
                .to_string(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "response": {
                        "type": "string",
                        "enum": ["yes", "no", "sort_of"],
                        "description": "Your answer to the question"
                    }
                },
                "required": ["response"]
            }),
        }
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        let resp = args.response.clone();
        let mut guard = self
            .action
            .lock()
            .map_err(|e| ToolCallError(e.to_string()))?;
        *guard = Some(SimAction::Answer(args.response));
        Ok(resp)
    }
}

// -- simulator correct_answer tool --

#[derive(Deserialize)]
struct CorrectAnswerArgs {}

struct SimCorrectAnswerTool {
    action: SharedAction,
}

impl Tool for SimCorrectAnswerTool {
    const NAME: &'static str = "correct_answer";
    type Error = ToolCallError;
    type Args = CorrectAnswerArgs;
    type Output = String;

    async fn definition(&self, _prompt: String) -> ToolDefinition {
        ToolDefinition {
            name: "correct_answer".to_string(),
            description: "The player correctly guessed the secret.".to_string(),
            parameters: json!({
                "type": "object",
                "properties": {}
            }),
        }
    }

    async fn call(&self, _args: Self::Args) -> Result<Self::Output, Self::Error> {
        let mut guard = self
            .action
            .lock()
            .map_err(|e| ToolCallError(e.to_string()))?;
        *guard = Some(SimAction::CorrectAnswer);
        Ok("correct".to_string())
    }
}

// -- simulator invalid_input tool --

#[derive(Deserialize)]
struct InvalidInputArgs {
    reason: String,
}

struct SimInvalidInputTool {
    action: SharedAction,
}

impl Tool for SimInvalidInputTool {
    const NAME: &'static str = "invalid_input";
    type Error = ToolCallError;
    type Args = InvalidInputArgs;
    type Output = String;

    async fn definition(&self, _prompt: String) -> ToolDefinition {
        ToolDefinition {
            name: "invalid_input".to_string(),
            description: "The player's input is not a valid yes/no question or guess. Does NOT consume a turn.".to_string(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why the input is invalid"
                    }
                },
                "required": ["reason"]
            }),
        }
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        let reason = args.reason.clone();
        let mut guard = self
            .action
            .lock()
            .map_err(|e| ToolCallError(e.to_string()))?;
        *guard = Some(SimAction::InvalidInput(args.reason));
        Ok(reason)
    }
}

// ---------------------------------------------------------------------------
// Simulator: single-completion approach
// ---------------------------------------------------------------------------

/// Issue a single completion request to the simulator agent and extract the
/// tool call from the response.
async fn sim_single_turn<M: CompletionModel>(
    sim: &impl Completion<M>,
    prompt: &str,
    history: Vec<Message>,
    sim_action: &SharedAction,
) -> anyhow::Result<()> {
    {
        let mut guard = sim_action.lock().unwrap();
        *guard = None;
    }

    let response: CompletionResponse<M::Response> = sim
        .completion(prompt, history)
        .await
        .map_err(|e| anyhow::anyhow!("Simulator completion build error: {e}"))?
        .send()
        .await
        .map_err(|e| anyhow::anyhow!("Simulator completion send error: {e}"))?;

    for content in response.choice.iter() {
        match content {
            AssistantContent::ToolCall(tc) => {
                let name = &tc.function.name;
                let args = &tc.function.arguments;
                match name.as_str() {
                    "answer" => {
                        let parsed: AnswerArgs = serde_json::from_value(args.clone())
                            .map_err(|e| anyhow::anyhow!("Failed to parse answer args: {e}"))?;
                        let mut guard = sim_action.lock().unwrap();
                        *guard = Some(SimAction::Answer(parsed.response));
                    }
                    "correct_answer" => {
                        let mut guard = sim_action.lock().unwrap();
                        *guard = Some(SimAction::CorrectAnswer);
                    }
                    "invalid_input" => {
                        let parsed: InvalidInputArgs = serde_json::from_value(args.clone())
                            .map_err(|e| {
                                anyhow::anyhow!("Failed to parse invalid_input args: {e}")
                            })?;
                        let mut guard = sim_action.lock().unwrap();
                        *guard = Some(SimAction::InvalidInput(parsed.reason));
                    }
                    other => {
                        log::warn!("Simulator called unknown tool: {other}");
                    }
                }
                break;
            }
            _ => continue,
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Guesser game tools: ask_yes_no_question and guess_answer
// ---------------------------------------------------------------------------

// Because Rig's Tool trait requires a concrete type for the simulator
// completion, we cannot use `impl Completion<M>` inside the tool struct.
// Instead, we use a closure-based approach: the game loop creates a callback
// that captures the simulator, and the tools invoke it through an Arc.

type SimCallback = Arc<dyn Fn(&str) -> SimCallbackFuture + Send + Sync>;
type SimCallbackFuture =
    std::pin::Pin<Box<dyn std::future::Future<Output = Result<String, ToolCallError>> + Send>>;

// -- ask_yes_no_question tool --

#[derive(Deserialize)]
struct AskYesNoQuestionArgs {
    question: String,
}

struct AskYesNoQuestionTool {
    game_state: SharedGameState,
    invoke_sim: SimCallback,
}

impl Tool for AskYesNoQuestionTool {
    const NAME: &'static str = "ask_yes_no_question";
    type Error = ToolCallError;
    type Args = AskYesNoQuestionArgs;
    type Output = String;

    async fn definition(&self, _prompt: String) -> ToolDefinition {
        ToolDefinition {
            name: "ask_yes_no_question".to_string(),
            description: "Ask a yes/no question to narrow down the answer. Uses one turn."
                .to_string(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "A yes/no question about the secret"
                    }
                },
                "required": ["question"]
            }),
        }
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        {
            let gs = self
                .game_state
                .lock()
                .map_err(|e| ToolCallError(e.to_string()))?;
            if gs.game_over {
                return Err(ToolCallError("game is already over".into()));
            }
        }

        // Log the guesser's question.
        {
            let mut gs = self
                .game_state
                .lock()
                .map_err(|e| ToolCallError(e.to_string()))?;
            gs.log_entries.push(LogEntry {
                timestamp: Utc::now(),
                player: Player::Guesser,
                content: args.question.clone(),
            });
        }
        log::info!("Guesser asks: {}", args.question);

        (self.invoke_sim)(&args.question).await
    }
}

// -- guess_answer tool --

#[derive(Deserialize)]
struct GuessAnswerArgs {
    answer: String,
}

struct GuessAnswerTool {
    game_state: SharedGameState,
    invoke_sim: SimCallback,
}

impl Tool for GuessAnswerTool {
    const NAME: &'static str = "guess_answer";
    type Error = ToolCallError;
    type Args = GuessAnswerArgs;
    type Output = String;

    async fn definition(&self, _prompt: String) -> ToolDefinition {
        ToolDefinition {
            name: "guess_answer".to_string(),
            description: "Make a guess at the answer. Uses one turn.".to_string(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "Your guess for the secret"
                    }
                },
                "required": ["answer"]
            }),
        }
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        {
            let gs = self
                .game_state
                .lock()
                .map_err(|e| ToolCallError(e.to_string()))?;
            if gs.game_over {
                return Err(ToolCallError("game is already over".into()));
            }
        }

        let guess_msg = format!("My answer is: {}", args.answer);

        // Log the guesser's guess.
        {
            let mut gs = self
                .game_state
                .lock()
                .map_err(|e| ToolCallError(e.to_string()))?;
            gs.log_entries.push(LogEntry {
                timestamp: Utc::now(),
                player: Player::Guesser,
                content: guess_msg.clone(),
            });
        }
        log::info!("Guesser guesses: {}", args.answer);

        (self.invoke_sim)(&guess_msg).await
    }
}

// ---------------------------------------------------------------------------
// Guesser exec tool (scratch container)
// ---------------------------------------------------------------------------

const SCRATCH_IMAGE: &str = "ubuntu:24.04";

#[derive(Deserialize)]
struct ExecArgs {
    cmd: Vec<String>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
}

fn default_timeout_ms() -> u64 {
    30_000
}

struct ExecTool {
    scratch: Arc<ScratchContainer>,
}

impl Tool for ExecTool {
    const NAME: &'static str = "exec";
    type Error = ToolCallError;
    type Args = ExecArgs;
    type Output = String;

    async fn definition(&self, _prompt: String) -> ToolDefinition {
        ToolDefinition {
            name: "exec".to_string(),
            description: "Execute a command in a scratch container. Use this to run code, \
                test hypotheses, or compute things during the game. Does NOT use a turn."
                .to_string(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "array",
                        "items": { "type": "string" },
                        "description": "Command and arguments to execute"
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (optional)"
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Timeout in milliseconds (default 30000)"
                    }
                },
                "required": ["cmd"]
            }),
        }
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        let cmd_str = args
            .cmd
            .iter()
            .map(|a| shell_escape(a))
            .collect::<Vec<_>>()
            .join(" ");

        let full_cmd = match args.cwd {
            Some(ref dir) => format!("cd {} && {}", shell_escape(dir), cmd_str),
            None => cmd_str,
        };

        let timeout_secs = (args.timeout_ms as f64 / 1000.0).ceil() as u64;
        let timed_cmd = format!("timeout {timeout_secs} sh -c {}", shell_escape(&full_cmd));

        log::debug!("ExecTool: {timed_cmd}");

        let result = self
            .scratch
            .exec(&timed_cmd)
            .await
            .map_err(|e| ToolCallError(format!("exec failed: {e}")))?;

        Ok(format!(
            "exit_code: {}\n{}",
            result.exit_code, result.output
        ))
    }
}

/// Minimal shell escaping: wraps in single quotes, escaping existing single quotes.
fn shell_escape(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

// ---------------------------------------------------------------------------
// Game config
// ---------------------------------------------------------------------------

struct GameConfig<'a> {
    eval_name: &'a str,
    model_name: &'a str,
    api: &'a str,
    first_message: &'a str,
    turn_limit: u32,
    calls_path: &'a Path,
    summary_path: &'a Path,
}

// ---------------------------------------------------------------------------
// Game loop
// ---------------------------------------------------------------------------

/// Run the 20 Questions game.
///
/// Creates a scratch container for the guesser's exec tool, builds both
/// agents, runs the game loop, and cleans up the container afterwards.
pub async fn run_game(
    model_name: &str,
    api: &str,
    variant: &Variant,
    output_dir: &Path,
) -> anyhow::Result<RunSummary> {
    let eval_name = format!("20q_rig_{}", variant.name);

    fs::create_dir_all(output_dir)?;
    let ts = Utc::now().format("%Y%m%d_%H%M%S");
    let calls_path = output_dir.join(format!("{eval_name}_{ts}_calls.jsonl"));
    let summary_path = output_dir.join(format!("{eval_name}_{ts}_summary.json"));

    let r = Runfiles::create().map_err(|e| anyhow::anyhow!("Failed to create runfiles: {e:?}"))?;

    let scratch_note = load_scratch_system_note(&r);
    let guesser_system = format!(
        "You are playing 20 Questions as the guesser. Ask strategic yes/no \
         questions to narrow down the answer. When confident, use the \
         guess_answer tool.\n\n{scratch_note}"
    );

    let sim_system = load_sim_prompt(&r, variant.turn_limit, variant.secret);

    let first_message = load_first_user_message(&r, variant.domain_description, variant.turn_limit);

    // Create the scratch container for the guesser's exec tool.
    log::info!("Creating scratch container ({SCRATCH_IMAGE})...");
    let scratch = Arc::new(ScratchContainer::create(SCRATCH_IMAGE).await?);
    log::info!("Scratch container ready: {}", scratch.container_id());

    let config = GameConfig {
        eval_name: &eval_name,
        model_name,
        api,
        first_message: &first_message,
        turn_limit: variant.turn_limit,
        calls_path: &calls_path,
        summary_path: &summary_path,
    };

    let result = match api {
        "openai" => {
            let client = rig::providers::openai::Client::from_env();
            run_with_client(
                &client,
                model_name,
                &guesser_system,
                &sim_system,
                &scratch,
                &config,
            )
            .await
        }
        "anthropic" => {
            let client = rig::providers::anthropic::Client::from_env();
            run_with_client(
                &client,
                model_name,
                &guesser_system,
                &sim_system,
                &scratch,
                &config,
            )
            .await
        }
        _ => anyhow::bail!("Unknown API provider: {api}. Use 'openai' or 'anthropic'."),
    };

    // Clean up the scratch container regardless of game outcome.
    log::info!("Cleaning up scratch container...");
    if let Err(e) = scratch.force_cleanup().await {
        log::warn!("Failed to clean up scratch container: {e}");
    }

    result
}

/// Build guesser and simulator agents from any provider client, then run the game loop.
async fn run_with_client<C: CompletionClient>(
    client: &C,
    model_name: &str,
    guesser_system: &str,
    sim_system: &str,
    scratch: &Arc<ScratchContainer>,
    config: &GameConfig<'_>,
) -> anyhow::Result<RunSummary>
where
    C::CompletionModel: 'static,
{
    // Shared state for capturing simulator tool calls.
    let sim_action: SharedAction = Arc::new(Mutex::new(None));

    // Shared game state for turn tracking.
    let game_state: SharedGameState = Arc::new(Mutex::new(GameState::new(config.turn_limit)));

    // Build the simulator agent (used internally by game tools).
    let sim = client
        .agent(model_name)
        .preamble(sim_system)
        .tool(SimAnswerTool {
            action: sim_action.clone(),
        })
        .tool(SimCorrectAnswerTool {
            action: sim_action.clone(),
        })
        .tool(SimInvalidInputTool {
            action: sim_action.clone(),
        })
        .tool_choice(ToolChoice::Required)
        .build();

    // Create the simulator callback that the guesser tools will invoke.
    // We wrap the simulator in an Arc so the closure can capture it.
    let sim = Arc::new(sim);
    let invoke_sim: SimCallback = {
        let sim = sim.clone();
        let game_state = game_state.clone();
        let sim_action = sim_action.clone();
        Arc::new(move |player_message: &str| {
            let sim = sim.clone();
            let game_state = game_state.clone();
            let sim_action = sim_action.clone();
            let msg = player_message.to_string();
            Box::pin(
                async move { invoke_simulator_erased(&*sim, &game_state, &sim_action, &msg).await },
            )
        })
    };

    // Build the guesser agent with game tools and exec tool.
    // tool_choice=Required forces the guesser to always use a tool.
    let guesser = client
        .agent(model_name)
        .preamble(guesser_system)
        .default_max_turns(100)
        .tool(AskYesNoQuestionTool {
            game_state: game_state.clone(),
            invoke_sim: invoke_sim.clone(),
        })
        .tool(GuessAnswerTool {
            game_state: game_state.clone(),
            invoke_sim: invoke_sim.clone(),
        })
        .tool(ExecTool {
            scratch: scratch.clone(),
        })
        .tool_choice(ToolChoice::Required)
        .build();

    run_game_loop(&guesser, &game_state, config).await
}

/// Type-erased simulator invocation so the closure doesn't need M as a parameter.
/// This works because the agent built by `client.agent(...).build()` implements
/// `Completion<M>` for its specific M, and we call it through the concrete type.
async fn invoke_simulator_erased<M: CompletionModel>(
    sim: &impl Completion<M>,
    game_state: &SharedGameState,
    sim_action: &SharedAction,
    player_message: &str,
) -> Result<String, ToolCallError> {
    let sim_history = {
        let gs = game_state
            .lock()
            .map_err(|e| ToolCallError(e.to_string()))?;
        gs.sim_history.clone()
    };

    sim_single_turn::<M>(sim, player_message, sim_history, sim_action)
        .await
        .map_err(|e| ToolCallError(format!("simulator error: {e}")))?;

    let action = {
        let guard = sim_action
            .lock()
            .map_err(|e| ToolCallError(e.to_string()))?;
        guard.clone()
    };

    let mut gs = game_state
        .lock()
        .map_err(|e| ToolCallError(e.to_string()))?;
    gs.sim_history.push(Message::user(player_message));

    match action {
        Some(SimAction::CorrectAnswer) => {
            gs.turns += 1;
            gs.game_over = true;
            gs.result = GameResult::Correct { turns: gs.turns };
            gs.sim_history.push(Message::assistant("correct_answer"));
            gs.log_entries.push(LogEntry {
                timestamp: Utc::now(),
                player: Player::Simulator,
                content: "correct_answer".into(),
            });
            log::info!("Simulator: correct_answer on turn {}", gs.turns);
            Ok("Correct! You guessed it!".to_string())
        }
        Some(SimAction::Answer(ref response)) => {
            gs.turns += 1;
            if gs.turns >= gs.turn_limit {
                gs.game_over = true;
            }
            gs.sim_history.push(Message::assistant(response));
            gs.log_entries.push(LogEntry {
                timestamp: Utc::now(),
                player: Player::Simulator,
                content: response.clone(),
            });
            log::info!("Simulator: {response} (turn {})", gs.turns);
            Ok(response.clone())
        }
        Some(SimAction::InvalidInput(ref reason)) => {
            gs.invalid_input_count += 1;
            let msg = format!("Invalid input: {reason}");
            gs.sim_history.push(Message::assistant(&msg));
            gs.log_entries.push(LogEntry {
                timestamp: Utc::now(),
                player: Player::Simulator,
                content: msg.clone(),
            });
            log::info!("Simulator: invalid_input ({reason}), turn not consumed");
            Ok(msg)
        }
        None => {
            gs.game_over = true;
            gs.log_entries.push(LogEntry {
                timestamp: Utc::now(),
                player: Player::Simulator,
                content: "(no tool action)".into(),
            });
            log::warn!("Simulator produced no tool action");
            Err(ToolCallError("simulator produced no tool action".into()))
        }
    }
}

/// Provider-agnostic game loop.
///
/// The guesser uses `Chat` with `tool_choice=Required`. Rig's agent loop
/// auto-executes tools (ask_yes_no_question, guess_answer, exec). The game
/// tools internally invoke the simulator and update shared game state.
async fn run_game_loop(
    guesser: &(impl Chat + Sync),
    game_state: &SharedGameState,
    config: &GameConfig<'_>,
) -> anyhow::Result<RunSummary> {
    // Send the first message to the guesser. The guesser's Chat loop will
    // auto-execute tool calls until it produces a text response or hits
    // max_turns. Each game tool call internally runs a simulator turn.
    let _guesser_response = guesser
        .chat(config.first_message, Vec::new())
        .await
        .map_err(|e| anyhow::anyhow!("Guesser error: {e}"))?;

    // Extract final state.
    let gs = game_state.lock().unwrap();

    // Write call log.
    let mut calls_file = fs::File::create(config.calls_path)?;
    for entry in &gs.log_entries {
        writeln!(calls_file, "{}", serde_json::to_string(entry)?)?;
    }

    let summary = RunSummary {
        eval_name: config.eval_name.to_string(),
        framework: "rig".into(),
        model: config.model_name.into(),
        api: config.api.into(),
        turns: gs.turns,
        invalid_input_count: gs.invalid_input_count,
        result: gs.result.clone(),
    };

    fs::write(config.summary_path, serde_json::to_string_pretty(&summary)?)?;
    log::info!(
        "Saved results to {}",
        config
            .summary_path
            .parent()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| ".".to_string())
    );

    Ok(summary)
}
