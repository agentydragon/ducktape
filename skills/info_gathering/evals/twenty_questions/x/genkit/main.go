// Package main implements the Twenty Questions game using Firebase Genkit (Go).
//
// Two LLM agents play 20 Questions: a guesser asks yes/no questions, and a
// simulator answers via Genkit tool calls (answer/correct_answer). Supports
// OpenAI and Anthropic models via Genkit plugins.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/bazelbuild/rules_go/go/runfiles"
	"github.com/firebase/genkit/go/genkit"
	anthropic "github.com/firebase/genkit/go/plugins/compat_oai/anthropic"
	oai "github.com/firebase/genkit/go/plugins/compat_oai/openai"
)

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		log.Fatalf("json.Marshal: %v", err)
	}
	return b
}

func mustJSONIndent(v any) []byte {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		log.Fatalf("json.MarshalIndent: %v", err)
	}
	return b
}

func main() {
	variant := flag.String("variant", "", "Game variant: states or wide")
	model := flag.String("model", "", "Model name (default depends on -api)")
	api := flag.String("api", "openai", "API provider: openai or anthropic")
	outputDir := flag.String("output-dir", "eval_results", "Output directory")
	scratch := flag.Bool("scratch", false, "Enable scratch container for guesser agent (requires Docker)")
	scratchImage := flag.String("scratch-image", "alpine:latest", "Docker image for scratch container")
	flag.Parse()

	if *variant == "" {
		log.Fatal("--variant is required (states or wide)")
	}

	if *model == "" {
		switch *api {
		case "anthropic":
			*model = "claude-haiku-4-5-20251001"
		default:
			*model = "gpt-4o-mini"
		}
	}

	v, err := getVariant(*variant)
	if err != nil {
		log.Fatal(err)
	}

	summary, err := runGame(*model, *api, v, *outputDir, *scratch, *scratchImage)
	if err != nil {
		log.Fatal(err)
	}

	fmt.Println(string(mustJSONIndent(summary)))
}

// Variant defines a game configuration.
type Variant struct {
	Name              string
	DomainDescription string
	Secret            string
	TurnLimit         int
}

func getVariant(name string) (Variant, error) {
	switch name {
	case "states":
		return Variant{
			Name:              "states",
			DomainDescription: "a US state",
			Secret:            "New Mexico",
			TurnLimit:         20,
		}, nil
	case "wide":
		return Variant{
			Name:              "wide",
			DomainDescription: "a thing — could be anything: object, place, concept, activity, anything",
			Secret:            "a sourdough starter",
			TurnLimit:         25,
		}, nil
	default:
		return Variant{}, fmt.Errorf("unknown variant: %s (choose 'states' or 'wide')", name)
	}
}

// LogEntry records a single turn in the game.
type LogEntry struct {
	Timestamp string          `json:"timestamp"`
	Player    string          `json:"player"`
	Content   string          `json:"content"`
	ToolCalls []toolCallEntry `json:"tool_calls,omitempty"`
}

// toolCallEntry records a tool invocation by the simulator.
type toolCallEntry struct {
	Name  string `json:"name"`
	Input string `json:"input"`
}

// GameResult is the discriminated union for game outcomes.
type GameResult struct {
	Kind  string `json:"kind"`
	Turns int    `json:"turns,omitempty"`
	Limit int    `json:"limit,omitempty"`
}

// RunSummary captures the full result of a game run.
type RunSummary struct {
	EvalName          string     `json:"eval_name"`
	Framework         string     `json:"framework"`
	Model             string     `json:"model"`
	API               string     `json:"api"`
	Turns             int        `json:"turns"`
	InvalidInputCount int        `json:"invalid_input_count"`
	Result            GameResult `json:"result"`
}

// genkitModelName returns the Genkit-format model name (provider/model).
func genkitModelName(api, model string) string {
	switch api {
	case "openai":
		return "openai/" + model
	case "anthropic":
		return "anthropic/" + model
	default:
		return model
	}
}

func runGame(modelName, api string, v Variant, outputDir string, scratchEnabled bool, scratchImage string) (*RunSummary, error) {
	ctx := context.Background()
	evalName := fmt.Sprintf("20q_genkit_%s", v.Name)

	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return nil, err
	}

	ts := time.Now().UTC().Format("20060102_150405")
	callsPath := filepath.Join(outputDir, fmt.Sprintf("%s_%s_calls.jsonl", evalName, ts))
	summaryPath := filepath.Join(outputDir, fmt.Sprintf("%s_%s_summary.json", evalName, ts))

	callsFile, err := os.Create(callsPath)
	if err != nil {
		return nil, err
	}
	defer callsFile.Close()

	// Initialize Genkit with the appropriate plugin.
	// genkit.Init panics on configuration errors rather than returning an error.
	fullModelName := genkitModelName(api, modelName)

	var g *genkit.Genkit
	switch api {
	case "openai":
		g = genkit.Init(
			ctx,
			genkit.WithPlugins(&oai.OpenAI{}),
			genkit.WithDefaultModel(fullModelName),
		)
	case "anthropic":
		g = genkit.Init(
			ctx,
			genkit.WithPlugins(&anthropic.Anthropic{}),
			genkit.WithDefaultModel(fullModelName),
		)
	default:
		return nil, fmt.Errorf("unsupported API provider: %s (use openai or anthropic)", api)
	}

	// Create scratch container if enabled.
	var scratch *ScratchContainer
	if scratchEnabled {
		log.Printf("Creating scratch container (image=%s)...", scratchImage)
		scratch, err = CreateScratchContainer(ctx, scratchImage)
		if err != nil {
			return nil, fmt.Errorf("creating scratch container: %w", err)
		}
		defer func() {
			log.Printf("Removing scratch container...")
			if rmErr := scratch.Remove(ctx); rmErr != nil {
				log.Printf("Warning: failed to remove scratch container: %v", rmErr)
			}
		}()
		log.Printf("Scratch container ready: %s", scratch.containerID[:12])
	}

	// Build the simulator system prompt from the shared template file.
	simSystem := loadSimPrompt(v)

	// Build the agent system prompt (with scratch note if enabled).
	agentSystem := buildAgentSystem(v, scratchEnabled)

	// Run the game loop.
	result, turns, invalidInputCount, err := runGameLoop(ctx, g, fullModelName, v, simSystem, agentSystem, callsFile, scratch)
	if err != nil {
		return nil, err
	}

	summary := &RunSummary{
		EvalName:          evalName,
		Framework:         "genkit",
		Model:             modelName,
		API:               api,
		Turns:             turns,
		InvalidInputCount: invalidInputCount,
		Result:            result,
	}

	if err := os.WriteFile(summaryPath, mustJSONIndent(summary), 0o644); err != nil {
		return nil, err
	}
	log.Printf("Saved results to %s", outputDir)

	return summary, nil
}

// loadRunfile reads a data file from Bazel runfiles, trimming whitespace.
func loadRunfile(rlocationPath string) string {
	path, err := runfiles.Rlocation(rlocationPath)
	if err != nil {
		log.Fatalf("Could not resolve runfile %s: %v", rlocationPath, err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		log.Fatalf("Could not read %s: %v", path, err)
	}
	return strings.TrimSpace(string(data))
}

func loadSimPrompt(v Variant) string {
	template := loadRunfile("_main/skills/info_gathering/evals/twenty_questions/sim.txt")
	r := strings.NewReplacer(
		"{turn_limit}", fmt.Sprintf("%d", v.TurnLimit),
		"{secret}", v.Secret,
	)
	return r.Replace(template)
}

func loadFirstUserMessage(v Variant) string {
	template := loadRunfile("_main/skills/info_gathering/evals/twenty_questions/first_user_message.txt")
	r := strings.NewReplacer(
		"{domain_description}", v.DomainDescription,
		"{turn_limit}", fmt.Sprintf("%d", v.TurnLimit),
	)
	return r.Replace(template)
}

func loadScratchSystemNote() string {
	return loadRunfile("_main/skills/info_gathering/evals/twenty_questions/scratch_system_note.txt")
}

// TODO: The Python implementations load SKILL.md and wrap it in XML tags via
// shared/prompts.py build_guesser_system. This Go version uses a simplified
// inline prompt since it doesn't load SKILL.md yet. Align with the canonical
// SKILL.md content if this becomes a maintained eval target.
func buildAgentSystem(_ Variant, scratchEnabled bool) string {
	base := "You are playing 20 Questions as the guesser. " +
		"Ask strategic yes/no questions to narrow down the answer. " +
		"Think about what categories and properties can efficiently divide " +
		"the remaining possibilities. When confident, use the guess_answer tool."
	if scratchEnabled {
		return base + "\n\n" + loadScratchSystemNote()
	}
	return base
}
