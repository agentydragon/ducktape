package main

import (
	"bytes"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
)

const maxTracePayloadBytes = 1 << 20

// traceRelayHandler forwards same-origin browser OTLP/HTTP exports only to the server-configured
// receiver. It deliberately uses an uninstrumented client so exporting a span cannot create an
// export span loop.
func traceRelayHandler() http.Handler {
	transport, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		transport = &http.Transport{Proxy: http.ProxyFromEnvironment, ForceAttemptHTTP2: true}
	} else {
		transport = transport.Clone()
	}
	transport.DialContext = (&net.Dialer{Timeout: 2 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		relayTraces(writer, request, client)
	})
}

func appHTTPHandler(next http.Handler) http.Handler {
	traced := observability.HTTPHandler(next)
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/telemetry/v1/traces" {
			// Like the Python implementation, keep OTLP export requests outside the server spans
			// that they can themselves cause to be exported.
			next.ServeHTTP(writer, request)
			return
		}
		traced.ServeHTTP(writer, request)
	})
}

func relayTraces(writer http.ResponseWriter, request *http.Request, client *http.Client) {
	contentType := strings.ToLower(strings.TrimSpace(strings.SplitN(request.Header.Get("Content-Type"), ";", 2)[0]))
	if contentType != "application/x-protobuf" {
		writeDetailError(writer, http.StatusUnsupportedMediaType, "expected application/x-protobuf")
		return
	}
	encoding := strings.ToLower(request.Header.Get("Content-Encoding"))
	if encoding != "" && encoding != "identity" {
		writeDetailError(writer, http.StatusUnsupportedMediaType, "compressed trace payloads are not accepted")
		return
	}
	endpoint := configuredTracesEndpoint()
	if endpoint == "" {
		writeDetailError(writer, http.StatusServiceUnavailable, "trace receiver is not configured")
		return
	}
	if request.ContentLength > maxTracePayloadBytes {
		writeDetailError(writer, http.StatusRequestEntityTooLarge, "trace payload is too large")
		return
	}
	payload, err := io.ReadAll(http.MaxBytesReader(writer, request.Body, maxTracePayloadBytes))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			writeDetailError(writer, http.StatusRequestEntityTooLarge, "trace payload is too large")
		} else {
			writeDetailError(writer, http.StatusBadRequest, "invalid trace payload")
		}
		return
	}
	upstreamRequest, err := http.NewRequestWithContext(request.Context(), http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		writeDetailError(writer, http.StatusBadGateway, "trace receiver unavailable")
		return
	}
	upstreamRequest.Header.Set("Content-Type", "application/x-protobuf")
	for name, value := range configuredTraceHeaders() {
		if !validTraceHeader(name, value) {
			writeDetailError(writer, http.StatusBadGateway, "trace receiver unavailable")
			return
		}
		upstreamRequest.Header.Set(name, value)
	}
	response, err := client.Do(upstreamRequest)
	if err != nil {
		writeDetailError(writer, http.StatusBadGateway, "trace receiver unavailable")
		return
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 64<<10))
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		writeDetailError(writer, http.StatusBadGateway, "trace receiver rejected the export")
		return
	}
	writer.WriteHeader(http.StatusOK)
}

func configuredTracesEndpoint() string {
	protocol := os.Getenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
	if protocol == "" {
		protocol = os.Getenv("OTEL_EXPORTER_OTLP_PROTOCOL")
	}
	if protocol == "" {
		protocol = "http/protobuf"
	}
	if !strings.EqualFold(protocol, "http/protobuf") {
		return ""
	}
	if endpoint := os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"); endpoint != "" {
		return endpoint
	}
	if endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"); endpoint != "" {
		return strings.TrimRight(endpoint, "/") + "/v1/traces"
	}
	return ""
}

func configuredTraceHeaders() map[string]string {
	encoded, ok := os.LookupEnv("OTEL_EXPORTER_OTLP_TRACES_HEADERS")
	if !ok {
		encoded = os.Getenv("OTEL_EXPORTER_OTLP_HEADERS")
	}
	headers := make(map[string]string)
	for _, pair := range strings.Split(encoded, ",") {
		name, value, found := strings.Cut(pair, "=")
		name = strings.TrimSpace(name)
		if !found || name == "" {
			continue
		}
		value = strings.TrimSpace(value)
		if decoded, err := url.PathUnescape(value); err == nil {
			value = decoded
		}
		headers[name] = value
	}
	return headers
}

func validTraceHeader(name, value string) bool {
	if name == "" {
		return false
	}
	for index := 0; index < len(name); index++ {
		character := name[index]
		if (character >= 'a' && character <= 'z') || (character >= 'A' && character <= 'Z') ||
			(character >= '0' && character <= '9') || strings.ContainsRune("!#$%&'*+-.^_`|~", rune(character)) {
			continue
		}
		return false
	}
	for index := 0; index < len(value); index++ {
		character := value[index]
		if (character < 0x20 && character != '\t') || character == 0x7f {
			return false
		}
	}
	return true
}
