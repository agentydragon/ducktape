package observability

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	metricspb "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	metricsdatapb "go.opentelemetry.io/proto/otlp/metrics/v1"
	"google.golang.org/grpc"
)

func TestHTTPProtocolExporterAndW3CPropagation(t *testing.T) {
	var mu sync.Mutex
	var exportPath, exportContentType, traceparent string
	collector := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_, _ = io.Copy(io.Discard, request.Body)
		mu.Lock()
		exportPath = request.URL.Path
		exportContentType = request.Header.Get("Content-Type")
		mu.Unlock()
		writer.WriteHeader(http.StatusOK)
	}))
	defer collector.Close()
	target := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		mu.Lock()
		traceparent = request.Header.Get("traceparent")
		mu.Unlock()
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer target.Close()

	for name, value := range map[string]string{
		"OTEL_SDK_DISABLED":                  "false",
		"OTEL_TRACES_EXPORTER":               "otlp",
		"OTEL_METRICS_EXPORTER":              "none",
		"OTEL_TRACES_SAMPLER":                "always_on",
		"OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "HTTP/PROTOBUF",
		"OTEL_EXPORTER_OTLP_PROTOCOL":        "grpc",
		"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": collector.URL + "/custom/trace-ingest",
		"OTEL_EXPORTER_OTLP_ENDPOINT":        collector.URL + "/fallback",
		"OTEL_EXPORTER_OTLP_TRACES_HEADERS":  "x-export-key=trace-test",
		"OTEL_EXPORTER_OTLP_HEADERS":         "x-fallback=ignored",
	} {
		t.Setenv(name, value)
	}
	shutdown, err := Init(context.Background(), "agentplane-app-test")
	if err != nil {
		t.Fatalf("Init() error = %v", err)
	}
	defer shutdown()

	ctx, span := Start(context.Background(), "parent")
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target.URL+"/private/path?secret=query", nil)
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Transport: HTTPTransport(http.DefaultTransport)}
	response, err := client.Do(request)
	if err != nil {
		t.Fatalf("instrumented request error = %v", err)
	}
	_ = response.Body.Close()
	span.End()
	shutdown()

	mu.Lock()
	defer mu.Unlock()
	if exportPath != "/custom/trace-ingest" {
		t.Errorf("trace export path = %q, want signal-specific endpoint path", exportPath)
	}
	if exportContentType != "application/x-protobuf" {
		t.Errorf("trace export content type = %q, want application/x-protobuf", exportContentType)
	}
	if !strings.HasPrefix(traceparent, "00-") {
		t.Errorf("outbound traceparent = %q, want W3C trace context", traceparent)
	}
}

type otlpMetricsReceiver struct {
	metricspb.UnimplementedMetricsServiceServer
	requests chan *metricspb.ExportMetricsServiceRequest
}

func (receiver *otlpMetricsReceiver) Export(
	_ context.Context, request *metricspb.ExportMetricsServiceRequest,
) (*metricspb.ExportMetricsServiceResponse, error) {
	receiver.requests <- request
	return &metricspb.ExportMetricsServiceResponse{}, nil
}

func TestOTLPMetricsExporterFlushesRuntimeAndCustomMetricsAtShutdown(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	receiver := &otlpMetricsReceiver{requests: make(chan *metricspb.ExportMetricsServiceRequest, 1)}
	server := grpc.NewServer()
	metricspb.RegisterMetricsServiceServer(server, receiver)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)

	for name, value := range map[string]string{
		"OTEL_SDK_DISABLED":                                 "false",
		"OTEL_TRACES_EXPORTER":                              "none",
		"OTEL_METRICS_EXPORTER":                             "otlp",
		"OTEL_SERVICE_NAME":                                 "action-service-test",
		"OTEL_RESOURCE_ATTRIBUTES":                          "service.namespace=agentplane,deployment.environment.name=test",
		"OTEL_EXPORTER_OTLP_PROTOCOL":                       "grpc",
		"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT":               "http://" + listener.Addr().String(),
		"OTEL_EXPORTER_OTLP_METRICS_INSECURE":               "true",
		"OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
	} {
		t.Setenv(name, value)
	}

	shutdown, err := Init(context.Background(), "fallback-service")
	if err != nil {
		t.Fatalf("Init() error = %v", err)
	}
	histogram, err := otel.Meter("test").Float64Histogram(
		"agentplane_mcp_oauth_token_request_duration_seconds",
		metric.WithDescription("MCP OAuth discovery and token endpoint request duration"),
		metric.WithUnit("s"),
	)
	if err != nil {
		t.Fatal(err)
	}
	histogram.Record(context.Background(), 0.125, metric.WithAttributes(
		attribute.String("operation", "exchange"), attribute.String("outcome", "success"),
	))
	shutdown()

	var request *metricspb.ExportMetricsServiceRequest
	select {
	case request = <-receiver.requests:
	case <-time.After(3 * time.Second):
		t.Fatal("metric provider shutdown did not flush to the OTLP receiver")
	}
	var foundHistogram bool
	var foundGoroutines, foundHeapAlloc, foundGCCount, foundGCPause bool
	var foundProcessCPU, foundProcessRSS, foundProcessFDs bool
	var instanceID string
	for _, resourceMetrics := range request.ResourceMetrics {
		resourceAttributes := make(map[string]string)
		if resourceMetrics.Resource != nil {
			for _, item := range resourceMetrics.Resource.Attributes {
				if item.Value != nil && item.Value.GetStringValue() != "" {
					resourceAttributes[item.Key] = item.Value.GetStringValue()
				}
			}
		}
		if resourceAttributes["service.name"] == "action-service-test" {
			instanceID = resourceAttributes["service.instance.id"]
			if resourceAttributes["service.namespace"] != "agentplane" ||
				resourceAttributes["deployment.environment.name"] != "test" {
				t.Errorf("metric resource attributes = %v, want deployment identity", resourceAttributes)
			}
		}
		for _, scope := range resourceMetrics.ScopeMetrics {
			for _, item := range scope.Metrics {
				switch item.Name {
				case "runtime.go.goroutines":
					foundGoroutines = true
				case "runtime.go.mem.heap_alloc":
					foundHeapAlloc = true
				case "runtime.go.gc.count":
					foundGCCount = true
				case "runtime.go.gc.pause_total":
					foundGCPause = true
				case "process.cpu.time":
					foundProcessCPU = true
				case "process.memory.rss":
					foundProcessRSS = true
				case "process.open_file_descriptors":
					foundProcessFDs = true
				case "agentplane_mcp_oauth_token_request_duration_seconds":
					foundHistogram = true
					if item.Unit != "s" || item.GetHistogram() == nil {
						t.Errorf("custom metric unit/type = %q/%T, want seconds histogram", item.Unit, item.Data)
						continue
					}
					histogram := item.GetHistogram()
					if histogram.AggregationTemporality != metricsdatapb.AggregationTemporality_AGGREGATION_TEMPORALITY_CUMULATIVE {
						t.Errorf("custom metric temporality = %v, want cumulative", histogram.AggregationTemporality)
					}
					if len(histogram.DataPoints) != 1 || len(histogram.DataPoints[0].ExplicitBounds) != 11 {
						t.Errorf("custom metric histogram = %v, want one point with Prometheus default buckets", histogram.DataPoints)
					} else if histogram.DataPoints[0].ExplicitBounds[0] != 0.005 || histogram.DataPoints[0].ExplicitBounds[10] != 10 {
						t.Errorf("custom metric histogram bounds = %v, want Prometheus default buckets", histogram.DataPoints[0].ExplicitBounds)
					}
					attributes := make(map[string]string)
					for _, attr := range histogram.DataPoints[0].Attributes {
						if attr.Value != nil {
							attributes[attr.Key] = attr.Value.GetStringValue()
						}
					}
					if attributes["operation"] != "exchange" || attributes["outcome"] != "success" {
						t.Errorf("custom metric attributes = %v, want operation=exchange outcome=success", attributes)
					}
				}
			}
		}
	}
	if instanceID == "" {
		t.Error("metric resource lacks a per-process service.instance.id")
	}
	if !foundHistogram || !foundGoroutines || !foundHeapAlloc || !foundGCCount || !foundGCPause ||
		!foundProcessCPU || !foundProcessRSS || !foundProcessFDs {
		t.Errorf("exported metric families missing: histogram=%v goroutines=%v heap_alloc=%v gc_count=%v gc_pause=%v process_cpu=%v process_rss=%v process_fds=%v",
			foundHistogram, foundGoroutines, foundHeapAlloc, foundGCCount, foundGCPause,
			foundProcessCPU, foundProcessRSS, foundProcessFDs)
	}
}

func TestOTLPMetricsConfigurationFailsClosed(t *testing.T) {
	for _, name := range []string{
		"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
		"OTEL_EXPORTER_OTLP_ENDPOINT",
		"OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
		"OTEL_EXPORTER_OTLP_PROTOCOL",
		"OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
	} {
		t.Setenv(name, "")
	}
	t.Setenv("OTEL_METRICS_EXPORTER", "otlp")
	if _, err := newMeterProvider(context.Background(), resource.Empty()); err == nil || !strings.Contains(err.Error(), "requires OTEL_EXPORTER_OTLP") {
		t.Errorf("newMeterProvider() with missing endpoint error = %v, want fail-closed endpoint error", err)
	}
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
	t.Setenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "delta")
	if _, err := newMeterProvider(context.Background(), resource.Empty()); err == nil || !strings.Contains(err.Error(), "requires cumulative temporality") {
		t.Errorf("newMeterProvider() with delta preference error = %v, want fail-closed temporality error", err)
	}
}
