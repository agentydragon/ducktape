package runner

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/runner/go/harness"
)

type environmentFlags []string

func (f *environmentFlags) String() string         { return strings.Join(*f, ",") }
func (f *environmentFlags) Set(value string) error { *f = append(*f, value); return nil }

// HarnessEnvironment constructs an explicit child environment. Nothing else from
// the runner process is inherited by a native harness.
func HarnessEnvironment(environ map[string]string, declared, inherited []string) (map[string]string, error) {
	child := map[string]string{}
	for _, name := range inherited {
		if strings.Contains(name, "=") {
			return nil, fmt.Errorf("--harness-inherit-env takes a bare NAME, got %q", name)
		}
	}
	for _, entry := range append(append([]string{}, inherited...), declared...) {
		name, value, hasValue := strings.Cut(entry, "=")
		if name == "" {
			return nil, errors.New("--harness-env expects NAME or NAME=value")
		}
		if hasValue {
			child[name] = value
		} else if value, ok := environ[name]; ok {
			child[name] = value
		}
	}
	for _, name := range inherited {
		if _, ok := child[name]; !ok {
			slog.Warn("inherited variable is absent from the runner environment", "name", name)
		}
	}
	return child, nil
}

func CLI(ctx context.Context, args []string) error {
	if filepath.Base(os.Args[0]) == "agentplane-runner-vm" || (len(args) > 0 && args[0] == "--guest") {
		config, control, ingress, err := GuestConfiguration("/run/agentplane/config.json", "/run/agentplane/launch.json")
		if err != nil {
			return err
		}
		runner, err := New(config)
		if err != nil {
			return err
		}
		return runner.Run(ctx, control, ingress)
	}
	flags := flag.NewFlagSet("agentplane-runner", flag.ContinueOnError)
	var config Config
	var control, ingress Endpoint
	var launchFile, instructionsFile, claudeBinary, codexBinary, anthropicURL, openaiURL string
	var declared, inherited environmentFlags
	var checkpoint DebugCheckpointConfig
	flags.StringVar(&config.StateDir, "state-dir", "", "Session logs and native persistence")
	flags.StringVar(&control.Target, "control-target", "", "Runner Gateway host:port")
	flags.StringVar(&control.Bearer, "control-bearer", "", "Control bearer placeholder")
	flags.StringVar(&ingress.Target, "ingress-target", "", "History ingress host:port")
	flags.StringVar(&ingress.Bearer, "ingress-bearer", "", "Ingress bearer placeholder")
	flags.StringVar(&launchFile, "launch-file", "", "Sandbox identity and bootstrap JSON")
	flags.StringVar(&instructionsFile, "platform-instructions-file", "", "Platform instructions")
	flags.StringVar(&claudeBinary, "claude-binary", "", "Claude Code executable")
	flags.StringVar(&anthropicURL, "anthropic-base-url", "", "Anthropic Messages endpoint")
	flags.StringVar(&codexBinary, "codex-binary", "", "Codex executable")
	flags.StringVar(&openaiURL, "openai-base-url", "", "OpenAI Responses base URL, including /v1")
	flags.Var(&declared, "harness-env", "NAME=value or NAME inherited by harnesses; repeatable")
	flags.Var(&inherited, "harness-inherit-env", "Image-owned environment NAME inherited by harnesses; repeatable")
	flags.StringVar(&checkpoint.Name, "test-debug-checkpoint-name", "", "Test-only durable checkpoint")
	flags.StringVar(&checkpoint.CommandID, "test-debug-checkpoint-command-id", "", "Test-only checkpoint command")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("unexpected positional arguments")
	}
	if config.StateDir == "" || control.Target == "" || control.Bearer == "" || ingress.Target == "" || ingress.Bearer == "" || launchFile == "" || instructionsFile == "" {
		return errors.New("state-dir, control-target, control-bearer, ingress-target, ingress-bearer, launch-file and platform-instructions-file are required")
	}
	launch, err := os.ReadFile(launchFile)
	if err != nil {
		return err
	}
	if err := DecodeStrict(launch, &config.Launch); err != nil {
		return fmt.Errorf("invalid launch file: %w", err)
	}
	identity := config.Launch.Sandbox
	if identity.Name == "" || identity.Namespace == "" || identity.UID == "" {
		return errors.New("Sandbox namespace, name and uid are required")
	}
	if config.Launch.Bootstrap != nil && *config.Launch.Bootstrap == "" {
		return errors.New("bootstrap must be nonempty when present")
	}
	instructions, err := os.ReadFile(instructionsFile)
	if err != nil {
		return err
	}
	config.Launch.PlatformInstructions = strings.TrimSpace(string(instructions))
	environ := map[string]string{}
	for _, entry := range os.Environ() {
		key, value, _ := strings.Cut(entry, "=")
		environ[key] = value
	}
	config.HarnessEnvironment, err = HarnessEnvironment(environ, declared, inherited)
	if err != nil {
		return err
	}
	if claudeBinary != "" {
		if anthropicURL == "" || environ["ANTHROPIC_AUTH_TOKEN"] == "" {
			return errors.New("claude-binary requires anthropic-base-url and ANTHROPIC_AUTH_TOKEN")
		}
		config.Claude = &harness.LaunchConfig{Binary: claudeBinary, BaseURL: anthropicURL, Token: environ["ANTHROPIC_AUTH_TOKEN"]}
	}
	if codexBinary != "" {
		if openaiURL == "" || environ["OPENAI_API_KEY"] == "" {
			return errors.New("codex-binary requires openai-base-url and OPENAI_API_KEY")
		}
		config.Codex = &harness.LaunchConfig{Binary: codexBinary, BaseURL: openaiURL, Token: environ["OPENAI_API_KEY"]}
	}
	if (checkpoint.Name == "") != (checkpoint.CommandID == "") {
		return errors.New("both test debug checkpoint options must be set together")
	}
	if checkpoint.Name != "" {
		config.DebugCheckpoint = &checkpoint
	}
	runner, err := New(config)
	if err != nil {
		return err
	}
	return runner.Run(ctx, control, ingress)
}
