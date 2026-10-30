package main

import (
	"context"
	"errors"
	"flag"

	"github.com/agentydragon/ducktape/agentplane/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/configuration"
)

func runService(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("agentplane-runner", flag.ContinueOnError)
	configPath := flags.String("config", "", "Versioned Runner configuration JSON")
	launchPath := flags.String("launch-file", "", "Sandbox identity and bootstrap JSON")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 || *configPath == "" || *launchPath == "" {
		return errors.New("--config and --launch-file are required; positional arguments are forbidden")
	}
	settings, ingress, err := configuration.Load(*configPath, *launchPath)
	if err != nil {
		return err
	}
	instance, err := runner.New(runner.Config{
		Control:                     settings.Control,
		StateDir:                    settings.StateDir,
		SessionReconcileConcurrency: settings.SessionReconcileConcurrency,
		Launch:                      settings.Launch,
		HarnessEnvironment:          settings.HarnessEnvironment,
		Claude:                      settings.Claude,
		Codex:                       settings.Codex,
		VMProcessPolicy:             settings.VMProcessPolicy,
		PodPID1:                     settings.PodPID1,
		CodexExecServer:             settings.CodexExecServer,
		WorkspaceRoot:               settings.WorkspaceRoot,
		InitializationCwd:           settings.InitializationCwd,
		DebugCheckpoint:             settings.DebugCheckpoint,
	})
	if err != nil {
		return err
	}
	return instance.Run(ctx, ingress)
}
