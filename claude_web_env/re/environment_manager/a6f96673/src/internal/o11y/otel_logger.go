// Reconstructed from binary a6f96673
// Source: internal/o11y/otel_logger.go
//
// This file contains the OpenTelemetry logging provider initialization,
// including OTLP HTTP log exporter configuration.

package o11y

import (
	"context"
	"log/slog"
	"time"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	"go.opentelemetry.io/otel/sdk/resource"
)

// o11yLoggingProvider wraps an OTel LoggerProvider.
//
// Binary: (*o11yLoggingProvider).Shutdown at 0xa54060
type o11yLoggingProvider struct {
	provider *sdklog.LoggerProvider
}

// Shutdown shuts down the underlying LoggerProvider.
// Binary address: 0xa54060
func (p *o11yLoggingProvider) Shutdown(ctx context.Context) error {
	if p.provider != nil {
		return p.provider.Shutdown(ctx)
	}
	return nil
}

// initOTelLogger initializes the OpenTelemetry logging pipeline.
// It creates an OTLP HTTP log exporter, a batch log processor,
// a LoggerProvider, and returns an otelslog bridge handler.
//
// Binary address: 0xa53300
// Source: otel_logger.go
//
// Internal closure functions:
//   - WithLoggerProvider.func9 at 0xa53920
//   - WithProcessor.func8 at 0xa539a0
//   - WithResource.func7 at 0xa53ba0
//   - WithMaxQueueSize.func6 at 0xa53c60
//   - WithExportTimeout.func5 at 0xa53d20
//   - WithExportMaxBatchSize.func4 at 0xa53de0
//   - WithExportInterval.func3 at 0xa53ea0
//   - WithHeaders.func1 at 0xa53f60
//   - WithInsecure.func2 at 0xa57280
func initOTelLogger(ctx context.Context, cfg *O11yConfig, res *resource.Resource) (*o11yLoggingProvider, slog.Handler, error) {
	endpoint := makeOTLPEndpoint(cfg.Endpoint, cfg.APIKey, "logs")

	headers := map[string]string{
		"dd-api-key":  cfg.APIKey,
		"dd-protocol": cfg.Environment,
	}

	opts := []otlploghttp.Option{
		otlploghttp.WithEndpointURL(endpoint),
		otlploghttp.WithHeaders(headers),
	}

	if cfg.Insecure {
		opts = append(opts, otlploghttp.WithInsecure())
	}

	exporter, err := otlploghttp.New(ctx, opts...)
	if err != nil {
		return nil, nil, err
	}

	processor := sdklog.NewBatchProcessor(exporter,
		sdklog.WithMaxQueueSize(2048),
		sdklog.WithExportTimeout(30*time.Second),
		sdklog.WithExportMaxBatchSize(512),
		sdklog.WithExportInterval(5*time.Second),
	)

	provider := sdklog.NewLoggerProvider(
		sdklog.WithProcessor(processor),
		sdklog.WithResource(res),
	)

	handler := otelslog.NewHandler("environment-runner",
		otelslog.WithLoggerProvider(provider),
	)

	return &o11yLoggingProvider{provider: provider}, handler, nil
}
