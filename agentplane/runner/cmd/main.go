package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/observability"
	runner "github.com/agentydragon/ducktape/agentplane/runner"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

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
		fmt.Fprintf(os.Stderr, "runner logging setup failed: %v\n", err)
		return 1
	}
	zap.ReplaceGlobals(logger)
	defer func() { _ = logger.Sync() }()

	if handled, code := runner.InternalMode(os.Args[1:]); handled {
		return code
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	shutdownTracing, err := observability.Init(ctx, "runner")
	if err != nil {
		zap.L().Error("runner tracing setup failed", zap.Error(err))
		return 1
	}
	defer shutdownTracing()
	err = runner.CLI(ctx, os.Args[1:])
	if err != nil {
		zap.L().Error("runner stopped", zap.Error(err))
		return 1
	}
	return 0
}
