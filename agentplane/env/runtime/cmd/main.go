package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/workspace"
	runtime "github.com/agentydragon/ducktape/agentplane/env/runtime"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"go.opentelemetry.io/otel/attribute"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

const vmResourcePropertiesMode = "--vm-resource-properties"

func main() {
	os.Exit(run())
}

func run() int {
	loggerConfig := zap.NewProductionConfig()
	loggerConfig.OutputPaths = []string{"stderr"}
	loggerConfig.ErrorOutputPaths = []string{"stderr"}
	loggerConfig.Sampling = nil
	loggerConfig.DisableCaller = true
	loggerConfig.DisableStacktrace = true
	loggerConfig.EncoderConfig.TimeKey = "time"
	loggerConfig.EncoderConfig.EncodeTime = zapcore.RFC3339NanoTimeEncoder
	loggerConfig.EncoderConfig.EncodeLevel = zapcore.CapitalLevelEncoder
	loggerConfig.EncoderConfig.EncodeDuration = zapcore.StringDurationEncoder
	loggerConfig.EncoderConfig.CallerKey = ""
	loggerConfig.EncoderConfig.StacktraceKey = ""
	logger, err := loggerConfig.Build()
	if err != nil {
		fmt.Fprintf(os.Stderr, "environment Runtime logging setup failed: %v\n", err)
		return 1
	}
	zap.ReplaceGlobals(logger)
	defer func() { _ = logger.Sync() }()

	if handled, code := internalMode(os.Args[1:]); handled {
		return code
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if err := runService(ctx, os.Args[1:]); err != nil {
		zap.L().Error("environment Runtime stopped", zap.Error(err))
		return 1
	}
	return 0
}

func internalMode(args []string) (bool, int) {
	if len(args) == 0 {
		return false, 0
	}
	if args[0] == supervision.SupervisorMode {
		return true, supervision.SupervisorMain(args[1:])
	}
	if args[0] == workspace.HelperMode {
		return true, workspace.Main(args[1:])
	}
	if args[0] != vmResourcePropertiesMode {
		return false, 0
	}
	if len(args) != 2 || args[1] == "" {
		fmt.Fprintln(os.Stderr, "--vm-resource-properties requires one Runner config path")
		return true, 64
	}
	properties, err := configuration.GuestResourceSystemdProperties(args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return true, 1
	}
	for _, property := range properties {
		if _, err := fmt.Fprintln(os.Stdout, property); err != nil {
			return true, 1
		}
	}
	return true, 0
}

func runService(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("agentplane-environment-runtime", flag.ContinueOnError)
	configPath := flags.String("config", "", "Versioned environment configuration JSON")
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
	shutdownTracing, err := observability.Init(
		ctx, "environment-runtime", attribute.String("sandbox.uid", settings.Launch.Sandbox.UID),
	)
	if err != nil {
		return fmt.Errorf("initialize tracing: %w", err)
	}
	defer shutdownTracing()
	codexBinary := ""
	if settings.Codex != nil {
		codexBinary = settings.Codex.Binary
	}
	instance, err := runtime.New(runtime.Config{
		Control: settings.Control, StateDir: settings.StateDir, Launch: settings.Launch,
		Supervision: settings.Supervision, PodPID1: settings.PodPID1, VMProcessLauncher: settings.VMProcessLauncher,
		CodexExecServer: settings.CodexExecServer, CodexBinary: codexBinary,
		HarnessEnvironment: settings.HarnessEnvironment, InitializationCwd: settings.InitializationCwd,
		HostIngress: ingress,
		HostConfig: harnesshost.Config{
			StateDir: settings.StateDir, SessionReconcileConcurrency: settings.SessionReconcileConcurrency,
			Launch: settings.Launch, HarnessEnvironment: settings.HarnessEnvironment,
			Claude: settings.Claude, Codex: settings.Codex, VMProcessLauncher: settings.VMProcessLauncher,
			CodexAppServerReadiness: settings.CodexAppServerReadiness,
			WorkspaceRoot:           settings.WorkspaceRoot, InitializationCwd: settings.InitializationCwd,
			DebugCheckpoint: settings.DebugCheckpoint,
		},
	})
	if err != nil {
		return err
	}
	defer func() {
		if err := instance.Close(context.Background()); err != nil {
			zap.L().Error("environment Runtime cleanup failed", zap.Error(err))
		}
	}()
	return instance.Run(ctx)
}
