package main

import (
	"context"
	"errors"
	"flag"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost"
)

type serviceConfiguration struct {
	settings        configuration.Resolved
	ingress         configuration.Endpoint
	runtimeSocket   string
	hostSocket      string
	executionSocket string
}

func loadServiceConfiguration(args []string) (serviceConfiguration, bool, error) {
	flags := flag.NewFlagSet("agentplane-harness-host", flag.ContinueOnError)
	configPath := flags.String("config", "", "Versioned Runner configuration JSON")
	supervisionMode := flags.String("supervision", "", "Native process backend: subprocess or systemd")
	runtimeSocket := flags.String("runtime-socket", "", "Environment Runtime lifecycle socket")
	hostSocket := flags.String("listen-socket", "", "Private native Session RPC socket")
	executionSocket := flags.String("execution-socket", "", "Execution service health socket")
	launchPath := flags.String("launch-file", "", "Sandbox identity and bootstrap JSON")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return serviceConfiguration{}, true, nil
		}
		return serviceConfiguration{}, false, err
	}
	if flags.NArg() != 0 || *configPath == "" || *launchPath == "" || *runtimeSocket == "" || *hostSocket == "" {
		return serviceConfiguration{}, false, errors.New("--config, --launch-file, --runtime-socket and --listen-socket are required; positional arguments are forbidden")
	}
	settings, ingress, err := configuration.Load(*configPath, *launchPath)
	if err != nil {
		return serviceConfiguration{}, false, err
	}
	switch *supervisionMode {
	case "subprocess":
		if settings.VMProcessLauncher != nil {
			return serviceConfiguration{}, false, errors.New("subprocess supervision cannot use VM process settings")
		}
	case "systemd":
		if settings.VMProcessLauncher == nil {
			return serviceConfiguration{}, false, errors.New("systemd supervision requires VM process settings")
		}
	default:
		return serviceConfiguration{}, false, errors.New("--supervision must be subprocess or systemd")
	}
	return serviceConfiguration{
		settings: settings, ingress: ingress,
		runtimeSocket: *runtimeSocket, hostSocket: *hostSocket, executionSocket: *executionSocket,
	}, false, nil
}

func runService(ctx context.Context, config serviceConfiguration) error {
	settings := config.settings
	execServerURL := ""
	if settings.CodexExecServer != nil {
		execServerURL = settings.CodexExecServer.URL
	}
	instance, err := harnesshost.New(harnesshost.Config{
		RuntimeSocket: config.runtimeSocket, ListenSocket: config.hostSocket, ExecutionSocket: config.executionSocket,
		StateDir:                    settings.StateDir,
		SessionReconcileConcurrency: settings.SessionReconcileConcurrency,
		Launch:                      settings.Launch,
		HarnessEnvironment:          settings.HarnessEnvironment,
		Claude:                      settings.Claude,
		Codex:                       settings.Codex,
		VMProcessLauncher:           settings.VMProcessLauncher,
		ExecServerURL:               execServerURL,
		CodexAppServerReadiness:     settings.CodexAppServerReadiness,
		WorkspaceRoot:               settings.WorkspaceRoot,
		InitializationCwd:           settings.InitializationCwd,
		DebugCheckpoint:             settings.DebugCheckpoint,
	})
	if err != nil {
		return err
	}
	return instance.Run(ctx, config.ingress)
}
