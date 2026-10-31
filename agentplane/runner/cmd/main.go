package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"go.opentelemetry.io/otel/attribute"
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

	if handled, code := internalMode(os.Args[1:]); handled {
		return code
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	settings, ingress, handled, err := loadServiceConfiguration(os.Args[1:])
	if err != nil {
		zap.L().Error("runner configuration load failed", zap.Error(err))
		return 1
	}
	if handled {
		return 0
	}
	shutdownTracing, err := observability.Init(
		ctx, "runner", attribute.String("sandbox.uid", settings.Launch.Sandbox.UID),
	)
	if err != nil {
		zap.L().Error("runner tracing setup failed", zap.Error(err))
		return 1
	}
	defer shutdownTracing()
	err = runService(ctx, settings, ingress)
	if err != nil {
		zap.L().Error("runner stopped", zap.Error(err))
		return 1
	}
	return 0
}
