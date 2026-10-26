// Package observability provides privacy-conscious OpenTelemetry tracing for Agentplane Go services.
package observability

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/exporters/stdout/stdouttrace"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
)

const (
	shutdownTimeout = 5 * time.Second
	tracerName      = "github.com/agentydragon/ducktape/agentplane/observability"
)

var traceContext = propagation.TraceContext{}

// Init configures the standard OTEL_* trace exporters. serviceName is used when OTEL_SERVICE_NAME
// is absent. Shutdown is idempotent and flushes spans.
func Init(ctx context.Context, serviceName string) (func(), error) {
	if strings.EqualFold(strings.TrimSpace(os.Getenv("OTEL_SDK_DISABLED")), "true") {
		return func() {}, nil
	}

	name := os.Getenv("OTEL_SERVICE_NAME")
	if name == "" {
		name = serviceName
	}
	res, err := resource.New(
		ctx,
		resource.WithFromEnv(),
		resource.WithTelemetrySDK(),
		resource.WithAttributes(attribute.String("service.name", name)),
	)
	if err != nil {
		return nil, fmt.Errorf("create trace resource: %w", err)
	}
	sampler, err := configuredSampler()
	if err != nil {
		return nil, err
	}
	exporterNames, explicitlySet := os.LookupEnv("OTEL_TRACES_EXPORTER")
	if !explicitlySet {
		exporterNames = "otlp"
	}
	exporters := make([]sdktrace.SpanExporter, 0, 2)
	if !containsExporter(strings.Split(exporterNames, ","), "none") {
		seen := make(map[string]struct{})
		for _, rawName := range strings.Split(exporterNames, ",") {
			exporterName := strings.ToLower(strings.TrimSpace(rawName))
			if _, ok := seen[exporterName]; ok {
				continue
			}
			seen[exporterName] = struct{}{}
			switch exporterName {
			case "otlp":
				exporter, exportErr := newOTLPExporter(ctx)
				if exportErr != nil {
					shutdownExporters(exporters)
					return nil, exportErr
				}
				exporters = append(exporters, exporter)
			case "console":
				exporter, exportErr := stdouttrace.New()
				if exportErr != nil {
					shutdownExporters(exporters)
					return nil, fmt.Errorf("create console trace exporter: %w", exportErr)
				}
				exporters = append(exporters, exporter)
			default:
				shutdownExporters(exporters)
				return nil, fmt.Errorf("unsupported OTEL_TRACES_EXPORTER value %q", exporterName)
			}
		}
	}
	providerOptions := []sdktrace.TracerProviderOption{sdktrace.WithResource(res), sdktrace.WithSampler(sampler)}
	for _, exporter := range exporters {
		providerOptions = append(providerOptions, sdktrace.WithBatcher(exporter))
	}
	provider := sdktrace.NewTracerProvider(providerOptions...)
	otel.SetTracerProvider(provider)
	otel.SetTextMapPropagator(traceContext)

	var once sync.Once
	return func() {
		once.Do(func() {
			shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
			defer cancel()
			if err := provider.Shutdown(shutdownCtx); err != nil {
				slog.Warn("OpenTelemetry trace shutdown failed", "error", err)
			}
		})
	}, nil
}

func containsExporter(names []string, wanted string) bool {
	for _, name := range names {
		if strings.EqualFold(strings.TrimSpace(name), wanted) {
			return true
		}
	}
	return false
}

func newOTLPExporter(ctx context.Context) (sdktrace.SpanExporter, error) {
	protocol := os.Getenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
	if protocol == "" {
		protocol = os.Getenv("OTEL_EXPORTER_OTLP_PROTOCOL")
	}
	if protocol == "" {
		protocol = "http/protobuf"
	}
	switch strings.ToLower(protocol) {
	case "grpc":
		exporter, err := otlptracegrpc.New(ctx)
		if err != nil {
			return nil, fmt.Errorf("create OTLP trace exporter: %w", err)
		}
		return exporter, nil
	case "http/protobuf":
		exporter, err := otlptracehttp.New(ctx)
		if err != nil {
			return nil, fmt.Errorf("create OTLP trace exporter: %w", err)
		}
		return exporter, nil
	default:
		return nil, fmt.Errorf("unsupported OTEL_EXPORTER_OTLP_TRACES_PROTOCOL/OTEL_EXPORTER_OTLP_PROTOCOL value %q", protocol)
	}
}

func shutdownExporters(exporters []sdktrace.SpanExporter) {
	for _, exporter := range exporters {
		_ = exporter.Shutdown(context.Background())
	}
}

func configuredSampler() (sdktrace.Sampler, error) {
	name := strings.ToLower(os.Getenv("OTEL_TRACES_SAMPLER"))
	if name == "" {
		name = "parentbased_always_on"
	}
	if name == "always_on" {
		return sdktrace.AlwaysSample(), nil
	}
	if name == "always_off" {
		return sdktrace.NeverSample(), nil
	}
	if name != "traceidratio" && name != "parentbased_traceidratio" {
		if name == "parentbased_always_on" {
			return sdktrace.ParentBased(sdktrace.AlwaysSample()), nil
		}
		if name == "parentbased_always_off" {
			return sdktrace.ParentBased(sdktrace.NeverSample()), nil
		}
		return nil, fmt.Errorf("unsupported OTEL_TRACES_SAMPLER value %q", name)
	}
	ratio := 1.0
	if value := os.Getenv("OTEL_TRACES_SAMPLER_ARG"); value != "" {
		parsed, err := strconv.ParseFloat(value, 64)
		if err != nil || !(parsed >= 0 && parsed <= 1) {
			return nil, fmt.Errorf("OTEL_TRACES_SAMPLER_ARG must be between 0 and 1")
		}
		ratio = parsed
	}
	root := sdktrace.TraceIDRatioBased(ratio)
	if name == "traceidratio" {
		return root, nil
	}
	return sdktrace.ParentBased(root), nil
}

// Start creates a span with the Agentplane tracer.
func Start(ctx context.Context, name string, options ...trace.SpanStartOption) (context.Context, trace.Span) {
	return otel.Tracer(tracerName).Start(ctx, name, options...)
}

// End records only the operation outcome; it deliberately omits the error message, which may contain request data.
func End(span trace.Span, err error) {
	if err != nil {
		span.SetStatus(codes.Error, "operation failed")
	}
	span.End()
}

// Traceparent serializes the active W3C trace context for durable asynchronous work.
func Traceparent(ctx context.Context) string {
	carrier := propagation.MapCarrier{}
	traceContext.Inject(ctx, carrier)
	return carrier.Get("traceparent")
}

// WithTraceparent restores a W3C trace context saved with asynchronous work.
func WithTraceparent(ctx context.Context, value string) context.Context {
	if value == "" {
		return ctx
	}
	return traceContext.Extract(ctx, propagation.MapCarrier{"traceparent": value})
}

// GRPCServerOption instruments inbound gRPC calls and propagates W3C trace context.
func GRPCServerOption() grpc.ServerOption {
	return grpc.StatsHandler(otelgrpc.NewServerHandler())
}

// GRPCClientOption instruments outbound gRPC calls and propagates W3C trace context.
func GRPCClientOption() grpc.DialOption {
	return grpc.WithStatsHandler(otelgrpc.NewClientHandler())
}

// HTTPHandler instruments inbound HTTP requests without recording URLs, query strings, headers, or bodies.
func HTTPHandler(next http.Handler) http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		ctx := traceContext.Extract(request.Context(), propagation.HeaderCarrier(request.Header))
		ctx, span := Start(
			ctx, "HTTP "+request.Method,
			trace.WithSpanKind(trace.SpanKindServer),
			trace.WithAttributes(attribute.String("http.request.method", request.Method)),
		)
		response := &statusWriter{ResponseWriter: writer}
		defer func() {
			code := response.status
			if code == 0 {
				code = http.StatusOK
			}
			span.SetAttributes(attribute.Int("http.response.status_code", code))
			if code >= http.StatusInternalServerError {
				span.SetStatus(codes.Error, "server error")
			}
			span.End()
		}()
		instrumentedRequest := request.Clone(ctx)
		next.ServeHTTP(response, instrumentedRequest)
		if route := instrumentedRequest.Pattern; route != "" {
			if separator := strings.IndexByte(route, ' '); separator >= 0 {
				route = route[separator+1:]
			}
			span.SetAttributes(attribute.String("http.route", route))
		}
	})
}

// HTTPTransport instruments outbound HTTP requests without recording URLs, query strings, headers, or bodies.
func HTTPTransport(next http.RoundTripper) http.RoundTripper {
	if next == nil {
		next = http.DefaultTransport
	}
	return roundTripperFunc(func(request *http.Request) (*http.Response, error) {
		ctx, span := Start(
			request.Context(), "HTTP "+request.Method,
			trace.WithSpanKind(trace.SpanKindClient),
			trace.WithAttributes(attribute.String("http.request.method", request.Method)),
		)
		outgoing := request.Clone(ctx)
		outgoing.Header = request.Header.Clone()
		if outgoing.Header == nil {
			outgoing.Header = make(http.Header)
		}
		traceContext.Inject(ctx, propagation.HeaderCarrier(outgoing.Header))
		response, err := next.RoundTrip(outgoing)
		if response != nil {
			span.SetAttributes(attribute.Int("http.response.status_code", response.StatusCode))
			if response.StatusCode >= http.StatusInternalServerError {
				span.SetStatus(codes.Error, "server error")
			}
		} else if err != nil {
			span.SetStatus(codes.Error, "request failed")
		}
		span.End()
		return response, err
	})
}

// WrapHTTPTransport composes an existing transport wrapper with trace propagation for client-go.
func WrapHTTPTransport(wrap func(http.RoundTripper) http.RoundTripper) func(http.RoundTripper) http.RoundTripper {
	return func(next http.RoundTripper) http.RoundTripper {
		if wrap != nil {
			next = wrap(next)
		}
		return HTTPTransport(next)
	}
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }

func (w *statusWriter) WriteHeader(status int) {
	if w.status != 0 {
		return
	}
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}

func (w *statusWriter) Write(data []byte) (int, error) {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	return w.ResponseWriter.Write(data)
}

func (w *statusWriter) Flush() {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *statusWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	hijacker, ok := w.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, errors.New("HTTP response writer does not support hijacking")
	}
	return hijacker.Hijack()
}

func (w *statusWriter) Push(target string, options *http.PushOptions) error {
	if pusher, ok := w.ResponseWriter.(http.Pusher); ok {
		return pusher.Push(target, options)
	}
	return http.ErrNotSupported
}

func (w *statusWriter) ReadFrom(reader io.Reader) (int64, error) {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	if readerFrom, ok := w.ResponseWriter.(io.ReaderFrom); ok {
		return readerFrom.ReadFrom(reader)
	}
	return io.Copy(struct{ io.Writer }{w.ResponseWriter}, reader)
}
