// Game logic for the Genkit Twenty Questions implementation.
//
// The guesser agent has tool_choice=required and three tools:
// ask_yes_no_question, guess_answer, and exec (scratch computation).
//
// The game tools (ask_yes_no_question, guess_answer) internally invoke the
// simulator (a single LLM call with answer/correct_answer/invalid_input tools)
// and return the result string to the guesser.
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/firebase/genkit/go/ai"
	"github.com/firebase/genkit/go/genkit"
)

// simAction is the discriminated union of simulator tool call results.
type simAction struct {
	Kind     string `json:"kind"`               // "answer", "correct_answer", or "invalid_input"
	Response string `json:"response,omitempty"` // "yes", "no", or "sort_of" (only for kind=answer)
	Reason   string `json:"reason,omitempty"`   // reason string (only for kind=invalid_input)
}

// answerInput is the schema for the simulator "answer" tool.
type answerInput struct {
	Response string `json:"response" jsonschema_description:"yes, no, or sort_of"`
}

// emptyInput is the schema for the "correct_answer" tool (no arguments).
type emptyInput struct{}

// invalidInputInput is the schema for the simulator "invalid_input" tool.
type invalidInputInput struct {
	Reason string `json:"reason" jsonschema_description:"Brief explanation of why the input is invalid"`
}

// askYesNoQuestionInput is the schema for the guesser "ask_yes_no_question" tool.
type askYesNoQuestionInput struct {
	Question string `json:"question" jsonschema_description:"A yes/no question about the secret"`
}

// guessAnswerInput is the schema for the guesser "guess_answer" tool.
type guessAnswerInput struct {
	Answer string `json:"answer" jsonschema_description:"Your guess for the secret"`
}

// execInput is the schema for the "exec" tool (scratch container execution).
type execInput struct {
	Cmd       []string `json:"cmd" jsonschema_description:"Command array passed to exec (no shell wrapping). For shell features use ['sh', '-c', '...']."`
	Cwd       *string  `json:"cwd,omitempty" jsonschema_description:"Working directory inside container (default: /work)"`
	TimeoutMs int      `json:"timeout_ms" jsonschema_description:"Timeout in milliseconds (default: 10000)"`
}

// gameState holds mutable state shared between guesser tools and the game loop.
type gameState struct {
	turns             int
	turnLimit         int
	invalidInputCount int
	gameOver          bool
	result            GameResult
	simHistory        []*ai.Message
	logEntries        []LogEntry
}

// runGameLoop runs the full twenty questions game loop using Genkit.
//
// The guesser agent uses tool_choice=required with ask_yes_no_question,
// guess_answer, and exec tools. The game tools internally invoke the
// simulator (answer/correct_answer/invalid_input) and return results.
func runGameLoop(
	ctx context.Context,
	g *genkit.Genkit,
	modelName string,
	v Variant,
	simSystem string,
	agentSystem string,
	callsFile *os.File,
	scratch *ScratchContainer,
) (GameResult, int, int, error) {
	// Shared game state.
	state := &gameState{
		turnLimit:  v.TurnLimit,
		result:     GameResult{Kind: "timeout", Limit: v.TurnLimit},
		simHistory: []*ai.Message{ai.NewSystemTextMessage(simSystem)},
	}

	// Define simulator tools (used internally by game tools).
	var lastAction *simAction
	var lastToolCalls []toolCallEntry

	simAnswerTool := genkit.DefineTool(
		g, "answer",
		"Answer the player's yes/no question with yes, no, or sort_of.",
		func(ctx *ai.ToolContext, input answerInput) (string, error) {
			if input.Response != "yes" && input.Response != "no" && input.Response != "sort_of" {
				return "", fmt.Errorf("invalid response %q: must be yes, no, or sort_of", input.Response)
			}
			lastAction = &simAction{Kind: "answer", Response: input.Response}
			lastToolCalls = append(lastToolCalls, toolCallEntry{Name: "answer", Input: string(mustJSON(input))})
			return fmt.Sprintf("Answered: %s", input.Response), nil
		},
	)

	simCorrectAnswerTool := genkit.DefineTool(
		g, "correct_answer",
		"The player correctly guessed the secret.",
		func(ctx *ai.ToolContext, input emptyInput) (string, error) {
			lastAction = &simAction{Kind: "correct_answer"}
			lastToolCalls = append(lastToolCalls, toolCallEntry{Name: "correct_answer", Input: "{}"})
			return "Correct answer acknowledged.", nil
		},
	)

	simInvalidInputTool := genkit.DefineTool(
		g, "invalid_input",
		"The player's input is not a valid yes/no question or guess. Does NOT consume a turn.",
		func(ctx *ai.ToolContext, input invalidInputInput) (string, error) {
			lastAction = &simAction{Kind: "invalid_input", Reason: input.Reason}
			lastToolCalls = append(lastToolCalls, toolCallEntry{Name: "invalid_input", Input: string(mustJSON(input))})
			return fmt.Sprintf("Invalid input: %s", input.Reason), nil
		},
	)

	// invokeSimulator runs a single simulator turn with the given player message.
	invokeSimulator := func(playerMessage string) (string, error) {
		state.simHistory = append(state.simHistory, ai.NewUserTextMessage(playerMessage))
		lastAction = nil
		lastToolCalls = nil

		simResp, err := genkit.Generate(
			ctx, g,
			ai.WithModelName(modelName),
			ai.WithMessages(state.simHistory...),
			ai.WithTools(simAnswerTool, simCorrectAnswerTool, simInvalidInputTool),
			ai.WithToolChoice(ai.ToolChoiceRequired),
		)
		if err != nil {
			return "", fmt.Errorf("simulator generate error: %w", err)
		}

		state.simHistory = simResp.History()

		if lastAction == nil {
			state.gameOver = true
			state.logEntries = append(state.logEntries, LogEntry{
				Timestamp: time.Now().UTC().Format(time.RFC3339),
				Player:    "simulator",
				Content:   "(no tool action)",
				ToolCalls: lastToolCalls,
			})
			return "", fmt.Errorf("simulator produced no tool call")
		}

		switch lastAction.Kind {
		case "correct_answer":
			state.turns++
			state.gameOver = true
			state.result = GameResult{Kind: "correct", Turns: state.turns}
			state.logEntries = append(state.logEntries, LogEntry{
				Timestamp: time.Now().UTC().Format(time.RFC3339),
				Player:    "simulator",
				Content:   "correct_answer",
				ToolCalls: lastToolCalls,
			})
			log.Printf("  Simulator: CORRECT! (turn %d)", state.turns)
			return "Correct! You guessed it!", nil

		case "answer":
			state.turns++
			if state.turns >= state.turnLimit {
				state.gameOver = true
			}
			state.logEntries = append(state.logEntries, LogEntry{
				Timestamp: time.Now().UTC().Format(time.RFC3339),
				Player:    "simulator",
				Content:   lastAction.Response,
				ToolCalls: lastToolCalls,
			})
			log.Printf("  Simulator: %s (turn %d)", lastAction.Response, state.turns)
			return lastAction.Response, nil

		case "invalid_input":
			// Does NOT consume a turn.
			state.invalidInputCount++
			msg := fmt.Sprintf("Invalid input: %s", lastAction.Reason)
			state.logEntries = append(state.logEntries, LogEntry{
				Timestamp: time.Now().UTC().Format(time.RFC3339),
				Player:    "simulator",
				Content:   msg,
				ToolCalls: lastToolCalls,
			})
			log.Printf("  Simulator: invalid_input (%s), turn not consumed", lastAction.Reason)
			return msg, nil

		default:
			return "", fmt.Errorf("unknown simulator action kind: %s", lastAction.Kind)
		}
	}

	// Define guesser game tools.
	askTool := genkit.DefineTool(
		g, "ask_yes_no_question",
		"Ask a yes/no question to narrow down the answer. Uses one turn.",
		func(toolCtx *ai.ToolContext, input askYesNoQuestionInput) (string, error) {
			if state.gameOver {
				return "", fmt.Errorf("game is already over")
			}

			state.logEntries = append(state.logEntries, LogEntry{
				Timestamp: time.Now().UTC().Format(time.RFC3339),
				Player:    "agent",
				Content:   input.Question,
			})
			log.Printf("  Guesser asks: %s", truncate(input.Question, 120))

			return invokeSimulator(input.Question)
		},
	)

	guessTool := genkit.DefineTool(
		g, "guess_answer",
		"Make a guess at the answer. Uses one turn.",
		func(toolCtx *ai.ToolContext, input guessAnswerInput) (string, error) {
			if state.gameOver {
				return "", fmt.Errorf("game is already over")
			}

			guessMsg := fmt.Sprintf("My answer is: %s", input.Answer)
			state.logEntries = append(state.logEntries, LogEntry{
				Timestamp: time.Now().UTC().Format(time.RFC3339),
				Player:    "agent",
				Content:   guessMsg,
			})
			log.Printf("  Guesser guesses: %s", input.Answer)

			return invokeSimulator(guessMsg)
		},
	)

	// Build guesser options with game tools.
	guesserOpts := []ai.GenerateOption{
		ai.WithTools(askTool, guessTool),
		ai.WithToolChoice(ai.ToolChoiceRequired),
	}

	// Add exec tool if scratch container is available.
	if scratch != nil {
		execTool := genkit.DefineTool(
			g, "exec",
			"Run a command in a private scratch container for computation, note-taking, or code execution. "+
				"Does NOT count as a question turn.",
			func(toolCtx *ai.ToolContext, input execInput) (string, error) {
				cwd := ""
				if input.Cwd != nil {
					cwd = *input.Cwd
				}
				timeoutMs := input.TimeoutMs
				if timeoutMs <= 0 {
					timeoutMs = 10000
				}
				result, err := scratch.Exec(ctx, input.Cmd, cwd, timeoutMs)
				if err != nil {
					return "", fmt.Errorf("exec failed: %w", err)
				}
				return fmt.Sprintf("exit_code=%d\n%s", result.ExitCode, result.Output), nil
			},
		)
		// Re-create tool list including exec.
		guesserOpts = []ai.GenerateOption{
			ai.WithTools(askTool, guessTool, execTool),
			ai.WithToolChoice(ai.ToolChoiceRequired),
		}
	}

	// Build the first user message for the guesser from the shared template.
	firstMessage := loadFirstUserMessage(v)

	// Guesser conversation history — starts with system + first user message.
	guesserHistory := []*ai.Message{
		ai.NewSystemTextMessage(agentSystem),
		ai.NewUserTextMessage(firstMessage),
	}

	writeLog := func() {
		for _, entry := range state.logEntries {
			fmt.Fprintln(callsFile, string(mustJSON(entry)))
		}
	}

	// Main loop: call guesser with required tool choice, Genkit auto-executes
	// the tool calls. Each game tool internally invokes the simulator.
	// We loop because the guesser may need multiple Generate calls if it
	// uses exec between game tool calls, or if the agent framework returns
	// after processing tool results.
	for !state.gameOver {
		log.Printf("Turn %d/%d", state.turns+1, v.TurnLimit)

		opts := append([]ai.GenerateOption{
			ai.WithModelName(modelName),
			ai.WithMessages(guesserHistory...),
		}, guesserOpts...)

		guesserResp, err := genkit.Generate(ctx, g, opts...)
		if err != nil {
			writeLog()
			return GameResult{}, state.turns, state.invalidInputCount, fmt.Errorf("guesser generate error: %w", err)
		}

		// Update guesser history for next iteration.
		guesserHistory = guesserResp.History()
	}

	writeLog()
	return state.result, state.turns, state.invalidInputCount, nil
}

// truncate shortens s to at most n runes, appending "..." if truncated.
func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	if n <= 3 {
		return string(r[:n])
	}
	return string(r[:n-3]) + "..."
}
