package observability

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
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
