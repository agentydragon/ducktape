package main

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func setTraceRelayEnv(t *testing.T) {
	t.Helper()
	for _, name := range []string{
		"OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
		"OTEL_EXPORTER_OTLP_PROTOCOL",
		"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
		"OTEL_EXPORTER_OTLP_ENDPOINT",
		"OTEL_EXPORTER_OTLP_TRACES_HEADERS",
		"OTEL_EXPORTER_OTLP_HEADERS",
	} {
		t.Setenv(name, "")
	}
}

func TestConfiguredTracesEndpoint(t *testing.T) {
	tests := []struct {
		name string
		set  func(*testing.T)
		want string
	}{
		{
			name: "signal endpoint wins and keeps its path",
			set: func(t *testing.T) {
				t.Setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")
				t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.test/custom")
				t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://fallback.test")
			},
			want: "https://collector.test/custom",
		},
		{
			name: "generic endpoint gets signal path",
			set: func(t *testing.T) {
				t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.test/base///")
			},
			want: "https://collector.test/base/v1/traces",
		},
		{
			name: "grpc protocol disables browser relay",
			set: func(t *testing.T) {
				t.Setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc")
				t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.test")
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			setTraceRelayEnv(t)
			test.set(t)
			if got := configuredTracesEndpoint(); got != test.want {
				t.Fatalf("configuredTracesEndpoint() = %q, want %q", got, test.want)
			}
		})
	}
}

func TestRelayTracesForwardsProtobufAndConfiguredHeaders(t *testing.T) {
	setTraceRelayEnv(t)
	body := []byte{0x0a, 0x00, 0xff, 0x01}
	var gotBody []byte
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/browser/traces" {
			t.Errorf("upstream path = %q", request.URL.Path)
		}
		if got := request.Header.Get("Content-Type"); got != "application/x-protobuf" {
			t.Errorf("upstream content type = %q", got)
		}
		if got := request.Header.Get("Authorization"); got != "Bearer signed+token" {
			t.Errorf("upstream authorization = %q", got)
		}
		if got := request.Header.Get("X-Tenant"); got != "browser" {
			t.Errorf("upstream tenant = %q", got)
		}
		gotBody, _ = io.ReadAll(request.Body)
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", upstream.URL+"/browser/traces")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=Bearer%20signed%2Btoken,X-Tenant=browser")

	request := httptest.NewRequest(http.MethodPost, "/telemetry/v1/traces", bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/x-protobuf; charset=binary")
	response := httptest.NewRecorder()
	traceRelayHandler().ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("relay status = %d; body = %s", response.Code, response.Body.String())
	}
	if !bytes.Equal(gotBody, body) {
		t.Fatalf("upstream body = %v, want %v", gotBody, body)
	}
	if response.Body.Len() != 0 {
		t.Fatalf("relay response body = %q, want empty", response.Body.String())
	}
}

func TestRelayTracesRejectsUnsupportedOrOversizedPayloads(t *testing.T) {
	tests := []struct {
		name        string
		contentType string
		encoding    string
		noEndpoint  bool
		body        []byte
		want        int
	}{
		{name: "unsupported content type", contentType: "application/json", want: http.StatusUnsupportedMediaType},
		{name: "compressed payload", contentType: "application/x-protobuf", encoding: "gzip", want: http.StatusUnsupportedMediaType},
		{name: "receiver not configured", contentType: "application/x-protobuf", noEndpoint: true, want: http.StatusServiceUnavailable},
		{name: "payload too large", contentType: "application/x-protobuf", body: bytes.Repeat([]byte("x"), maxTracePayloadBytes+1), want: http.StatusRequestEntityTooLarge},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			setTraceRelayEnv(t)
			if !test.noEndpoint {
				t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:1/v1/traces")
			}
			request := httptest.NewRequest(http.MethodPost, "/telemetry/v1/traces", bytes.NewReader(test.body))
			request.Header.Set("Content-Type", test.contentType)
			if test.encoding != "" {
				request.Header.Set("Content-Encoding", test.encoding)
			}
			response := httptest.NewRecorder()
			traceRelayHandler().ServeHTTP(response, request)
			if response.Code != test.want {
				t.Fatalf("relay status = %d; body = %s, want %d", response.Code, response.Body.String(), test.want)
			}
		})
	}
}

func TestRelayTracesMapsUpstreamRejectionToBadGateway(t *testing.T) {
	setTraceRelayEnv(t)
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusUnauthorized)
	}))
	defer upstream.Close()
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", upstream.URL)
	request := httptest.NewRequest(http.MethodPost, "/telemetry/v1/traces", bytes.NewReader([]byte("protobuf")))
	request.Header.Set("Content-Type", "application/x-protobuf")
	response := httptest.NewRecorder()
	traceRelayHandler().ServeHTTP(response, request)
	if response.Code != http.StatusBadGateway {
		t.Fatalf("relay status = %d; body = %s, want 502", response.Code, response.Body.String())
	}
}
