// Reconstructed from binary: Build ID 0b86a2a0 (release-1186d93b9-ext)
// Source: internal/o11y/otel_traces.go
//
// OTLP **trace** export is NEW in this build. Proof, from the binaries
// themselves (not inference):
//
//  1. The protobuf descriptors `opentelemetry/proto/trace/v1/trace.proto` and
//     `opentelemetry/proto/collector/trace/v1/trace_service.proto` are present
//     in this binary's runtime string set and ABSENT from the previous one
//     (release-d84d76b7-ext), which carries only `.../metrics/v1/...` and
//     `.../logs/v1/...`.
//  2. Struct tag `protobuf:"...,name=resource_spans,json=resourceSpans,..."`
//     exists only in this build; the previous build has `name=resource_logs`
//     and `name=resource_metrics` only. Same for `rejected_spans` vs. the older
//     `rejected_data_points` / `rejected_log_records`.
//  3. The exported method names `ExportSpans`, `UploadTraces`,
//     `RegisterSpanProcessor`, `UnregisterSpanProcessor`, `GetResourceSpans`,
//     `GetScopeSpans`, `GetRejectedSpans`, `GetParentSpanId`, `GetTraceState`
//     appear in this build's `.gopclntab` and in none of the previous build's.
//
// (The OTel *SDK* trace package was already linked into the previous binary —
// its self-observability metric descriptions are in both — but there was no
// exporter for it. What is new is the OTLP trace *pipeline*.)
//
// Transport is **HTTP/protobuf**, not gRPC. The exporter's client package
// (garbled `B7zCxbsfe`, 57 functions) contains
// `B7zCxbsfe.aP63dDwTjBzu.ApplyHTTPOption` / `(*aP63dDwTjBzu).ApplyHTTPOption`
// — `ApplyHTTPOption` is the exported `otlpconfig.GenericOption` method that
// only `otlptracehttp` links. `B7zCxbsfe.(*sQFVw8_3y).UploadTraces` at
// 0x1fdc900 is the `otlptrace.Client` implementation.
//
// Garbled package map for this build:
//
//	FKPKJ5B0zZ  -> internal/o11y                                (303 funcs)
//	c74YAfc     -> otel/exporters/otlp/otlptrace                (Exporter)
//	B7zCxbsfe   -> otel/exporters/otlp/otlptrace/otlptracehttp  (client)
//	KAc9jH1kaar -> otlptrace/internal/tracetransform            (9 funcs)
//	Vzx1w2MN    -> otel/sdk/trace                               (TracerProvider)
//	f2yl1Kh     -> otlp proto trace/v1
//	iDLEgR      -> otlp proto collector/trace/v1

package o11y

import (
	"context"
	"strings"

	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
)

// o11yTracingProvider wraps the SDK TracerProvider plus the single Tracer the
// service hands out.
//
// Binary: garbled type `fl_EoB` in package FKPKJ5B0zZ.
// Binary: (*fl_EoB).Shutdown at 0x2015420.
//
// Field layout is read off (*W2z7RY).Tracer at 0x200e080, which does
// `p := impl[+0x10]; if p == nil { return noopTracer() }; return p[+0x08], p[+0x10]`
// — i.e. the cached Tracer interface value lives at +0x08/+0x10 of this struct.
//
// TODO(re): the provider pointer's own offset (+0x00) and any extra fields are
// not confirmed; only +0x08/+0x10 (the trace.Tracer interface value) are.
type o11yTracingProvider struct {
	provider *sdktrace.TracerProvider // +0x00 // TODO(re): offset unconfirmed
	tracer   trace.Tracer             // +0x08 (itab) / +0x10 (data)
}

// Shutdown flushes and stops the tracer provider.
// Binary address: 0x2015420
func (p *o11yTracingProvider) Shutdown(ctx context.Context) error {
	if p == nil || p.provider == nil {
		return nil
	}
	return p.provider.Shutdown(ctx)
}

// newTracingProvider builds the OTLP/HTTP trace pipeline.
//
// Binary address: 0x20146a0 (FKPKJ5B0zZ.vKb8UqZJXTG)
//
// Confirmed call sequence inside that function:
//
//	0x2014957  strings.HasPrefix(endpoint, "http://")   // literal at .rodata
//	                                                    // 0x29143db, len 7
//	0x2014aad  callq 0x1fe1480  = otlptracehttp.New(ctx, opts...)
//	0x2014ce0  callq 0x1f91a00  = otel/sdk/trace helper (span processor)
//	0x2014d40  callq 0x1f8ec40  = otel/sdk/trace.NewTracerProvider
//	0x2014d61  callq 0x20170a0  = vKb8UqZJXTG.func2 (closure)
//	0x2014d80  callq 0x1f8f360  = (*sdktrace.TracerProvider).Tracer
//
// The "http://" prefix test is the classic insecure-endpoint check; the plaintext
// literal is the only unencrypted string in the function, so the endpoint value
// itself and the tracer/instrumentation name are garble-encrypted and were not
// recovered.
//
// TODO(re): option list (batching parameters, WithHeaders, resource attributes)
// not recovered — every option constructor argument is an encrypted literal.
// TODO(re): the instrumentation-scope name passed to .Tracer() is encrypted.
func newTracingProvider(ctx context.Context, endpoint string, headers map[string]string) (*o11yTracingProvider, error) {
	opts := []otlptracehttp.Option{
		otlptracehttp.WithEndpointURL(endpoint),
		otlptracehttp.WithHeaders(headers),
	}
	if strings.HasPrefix(endpoint, "http://") {
		opts = append(opts, otlptracehttp.WithInsecure())
	}

	exporter, err := otlptracehttp.New(ctx, opts...)
	if err != nil {
		return nil, err
	}

	// TODO(re): sdk/trace option list not reconstructed (sampler, resource,
	// batch timeouts). Only the fact that a TracerProvider is built from a
	// batching span processor over `exporter` is established.
	provider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
	)

	return &o11yTracingProvider{
		provider: provider,
		tracer:   provider.Tracer("environment-runner"), // TODO(re): name encrypted
	}, nil
}

// noopTracer returns the process-wide no-op Tracer used whenever no tracing
// provider is configured.
//
// Binary address: 0x2015720 (FKPKJ5B0zZ.hxFTE4). The function returns a
// constant itab (0x2bafb48) and a constant data pointer (0x3c1efa0), i.e. a
// singleton no-op — it does not consult the global OTel TracerProvider.
func noopTracer() trace.Tracer {
	return noop.NewTracerProvider().Tracer("")
}

// Tracer is the package-level convenience wrapper: resolve the singleton
// O11yService and return its Tracer.
//
// Binary address: 0x2015780 (FKPKJ5B0zZ.ZSY78l). It calls GetO11yService
// (0x200e0c0) and then dispatches through itab slot +0x38.
//
// That itab offset is itself evidence about the interface: a Go itab lays out
// fun[0] at +0x18, so +0x38 is fun[4] — the 5th method of a 5-method interface
// whose methods sort as Increment, LogHandler, RecordGauge, Shutdown, Tracer.
func Tracer(name string, tags []string) trace.Tracer {
	svc, _ := GetO11yService(name, tags)
	return svc.Tracer()
}

// mapCarrier is a propagation.TextMapCarrier over a plain map, new in this
// build alongside trace export.
//
// Binary: FKPKJ5B0zZ.nfjR4Fsm — Get at 0x20156a0, Set at 0x20156e0,
// Keys at 0x2015700 (pointer wrappers at 0x20249c0/0x2024a80/0x2024a40).
//
// TODO(re): bodies not disassembled; the map key/value types are assumed to be
// string/string from the TextMapCarrier contract.
type mapCarrier map[string]string

// Get returns the value for key.
// Binary address: 0x20156a0
func (c mapCarrier) Get(key string) string { return c[key] }

// Set stores value under key.
// Binary address: 0x20156e0
func (c mapCarrier) Set(key, value string) { c[key] = value }

// Keys lists the carrier's keys.
// Binary address: 0x2015700
func (c mapCarrier) Keys() []string {
	keys := make([]string, 0, len(c))
	for k := range c {
		keys = append(keys, k)
	}
	return keys
}

// ---------------------------------------------------------------------------
// Endpoint plumbing
// ---------------------------------------------------------------------------
//
// The three OTLP endpoints are handed to the process by the backend API client,
// not read from OTEL_* environment variables:
//
//	WWD9Ee6Wrf4m.(*DTYvuG8Sp).OtlpEndpoints  @ 0x130f480  (real implementation)
//	WWD9Ee6Wrf4m.(*A9gAF7IKUbk).OtlpEndpoints@ 0x1313260  (zero-value stub)
//
// The stub is the size oracle: it zeroes and returns **seven** machine words
// (`subq $0x38,%rsp`), whereas the previous binary's stub
// (vzkOwSfrP.(*TQqTx6).OtlpEndpoints @ 0x127b440) returned **five**
// (`subq $0x28,%rsp`). Two extra words = one extra `string`. So the result went
// from {logs, metrics, headers} to {logs, metrics, traces, headers} — a third
// OTLP endpoint appeared exactly when trace export appeared.
//
// TODO(re): the field ORDER of that return tuple is not established (which of
// the three strings is the traces endpoint), and the endpoint values in
// (*DTYvuG8Sp).OtlpEndpoints are built by inline garble literal decryptors whose
// key bytes come from a runtime global (0x3b2f460+0x18), so they were not
// statically decrypted.
//
// ---------------------------------------------------------------------------
// Wire payload (vendored OTLP protobuf types, all NEW in this build)
// ---------------------------------------------------------------------------
//
// These are the types behind the new `resource_spans` / `scope_spans` /
// `parent_span_id` / `trace_state` / `end_time_unix_nano` /
// `dropped_events_count` / `dropped_links_count` / `rejected_spans` json tags.
// Field numbers below are the binary's protobuf tags, read from RTTI.
//
//	ExportTraceServiceRequest   iDLEgR.D2LMHRrlack2  @0x2888060
//	  1  resource_spans   []*ResourceSpans
//	ExportTraceServiceResponse  iDLEgR.BM12eqH0      @0x2888120
//	  1  partial_success  *ExportTracePartialSuccess
//	ExportTracePartialSuccess   iDLEgR.LE0oUUGGetn   @0x289fd20
//	  1  rejected_spans   int64
//	  2  error_message    string
//	TracesData                  f2yl1Kh.N1GCKC       @0x28881e0
//	  1  resource_spans   []*ResourceSpans
//	ResourceSpans               f2yl1Kh.D5Aabn       @0x28b1920
//	  1  resource         *Resource
//	  2  scope_spans      []*ScopeSpans
//	  3  schema_url       string
//	ScopeSpans                  f2yl1Kh.WJPW6A9iTm   @0x28b1820
//	  1  scope            *InstrumentationScope
//	  2  spans            []*Span
//	  3  schema_url       string
//	Span                        f2yl1Kh.ScdUBJfJsbY  @0x28f0e00 (size 0x118)
//	  1  trace_id                 []byte
//	  2  span_id                  []byte
//	  3  trace_state              string
//	  4  parent_span_id           []byte
//	  5  name                     string
//	  6  kind                     Span_SpanKind
//	  7  start_time_unix_nano     uint64 (fixed64)
//	  8  end_time_unix_nano       uint64 (fixed64)
//	  9  attributes               []*KeyValue
//	  10 dropped_attributes_count uint32
//	  11 events                   []*Span_Event
//	  12 dropped_events_count     uint32
//	  13 links                    []*Span_Link
//	  14 dropped_links_count      uint32
//	  15 status                   *Status
//	  16 flags                    uint32 (fixed32)
//
// Transform layer: KAc9jH1kaar (tracetransform, 9 functions);
// KAc9jH1kaar.LkGKjYogkhw @0x1faad60 is Spans(), it groups sdk spans into a
// map[attribute.Distinct]*ResourceSpans and is called from
// c74YAfc.(*VVfGSQH1Cm).ExportSpans @0x1fad200.
