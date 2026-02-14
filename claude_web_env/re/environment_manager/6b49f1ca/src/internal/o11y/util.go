// Reconstructed from binary 6b49f1ca
// Source: internal/o11y/util.go
//
// Utility functions for endpoint construction and resource creation.

package o11y

import (
	"context"
	"fmt"

	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.24.0"
)

// makeOTLPEndpoint constructs the OTLP exporter endpoint URL.
// For "logs" signal type, the path is "/api/v2/logs".
// For "metrics" signal type, the path is "/api/v2/metrics".
// Otherwise, the path is "/api/v2/otlp".
//
// The format is: "%s/%s/%s" with the base endpoint, version, and signal path.
//
// Binary address: 0xa570e0
// Source: util.go
func makeOTLPEndpoint(endpoint string, apiKey string, signalType string) string {
	var path string
	switch signalType {
	case "logs":
		path = "/api/v2/logs"
	case "metrics":
		path = "/api/v2/metrics"
	default:
		path = "/api/v2/otlp"
	}
	return fmt.Sprintf("%s%s?dd-api-key=%s", endpoint, path, apiKey)
}

// newResource creates an OTel resource with the service name and
// OpenTelemetry schema URL.
//
// Binary address: 0xa52f40
// Source: util.go (originally in service.go based on binary call graph)
func newResource(ctx context.Context, cfg *O11yConfig) (*resource.Resource, error) {
	return resource.New(ctx,
		resource.WithAttributes(
			semconv.ServiceNameKey.String(cfg.ServiceName),
		),
		resource.WithSchemaURL("https://opentelemetry.io/schemas/1.37.0"),
	)
}
