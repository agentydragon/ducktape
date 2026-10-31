package main

import (
	"context"
	"errors"
	"flag"

	"github.com/agentydragon/ducktape/agentplane/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/configuration"
)

func loadServiceConfiguration(args []string) (configuration.Resolved, configuration.Endpoint, bool, error) {
	flags := flag.NewFlagSet("agentplane-runner", flag.ContinueOnError)
	configPath := flags.String("config", "", "Versioned Runner configuration JSON")
	launchPath := flags.String("launch-file", "", "Sandbox identity and bootstrap JSON")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return configuration.Resolved{}, configuration.Endpoint{}, true, nil
		}
		return configuration.Resolved{}, configuration.Endpoint{}, false, err
	}
	if flags.NArg() != 0 || *configPath == "" || *launchPath == "" {
		return configuration.Resolved{}, configuration.Endpoint{}, false, errors.New("--config and --launch-file are required; positional arguments are forbidden")
	}
	settings, ingress, err := configuration.Load(*configPath, *launchPath)
	if err != nil {
		return configuration.Resolved{}, configuration.Endpoint{}, false, err
	}
	return settings, ingress, false, nil
}

func runService(ctx context.Context, settings configuration.Resolved, ingress configuration.Endpoint) error {
	instance, err := runner.New(runner.Config{
		Control:                     settings.Control,
		StateDir:                    settings.StateDir,
		SessionReconcileConcurrency: settings.SessionReconcileConcurrency,
		Launch:                      settings.Launch,
		HarnessEnvironment:          settings.HarnessEnvironment,
		Claude:                      settings.Claude,
		Codex:                       settings.Codex,
		VMProcessLauncher:           settings.VMProcessLauncher,
		PodPID1:                     settings.PodPID1,
		CodexExecServer:             settings.CodexExecServer,
		CodexAppServerReadiness:     settings.CodexAppServerReadiness,
		WorkspaceRoot:               settings.WorkspaceRoot,
		InitializationCwd:           settings.InitializationCwd,
		DebugCheckpoint:             settings.DebugCheckpoint,
	})
	if err != nil {
		return err
	}
	return instance.Run(ctx, ingress)
}
