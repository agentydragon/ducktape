// Reconstructed from binary 495ea204
// Source: internal/o11y/service.go

package o11y

import (
	"context"
	"log/slog"
	"sync"

	"go.opentelemetry.io/otel/trace"
)

// O11yService is the interface for the observability service, providing
// metric increment/gauge recording, logging handler, tracing, and shutdown.
//
// Binary itab: go:itab.DiscardO11yService,O11yService at 0xf65430
//
// METHOD SET, established from Build ID 0b86a2a0's .gopclntab: both
// implementations expose exactly five methods —
// Increment, LogHandler, RecordGauge, Shutdown, Tracer:
//
//	FKPKJ5B0zZ.(*W2z7RY).{Increment,LogHandler,RecordGauge,Shutdown,Tracer}
//	FKPKJ5B0zZ.(*NR2MTNm).{Increment,LogHandler,RecordGauge,Shutdown,Tracer}
//
// Corroborated by the itab dispatch in FKPKJ5B0zZ.ZSY78l (0x2015780), which
// calls through slot +0x38: a Go itab puts fun[0] at +0x18, so +0x38 is fun[4],
// the fifth and last method in sorted order.
//
// The previous binary (release-d84d76b7-ext) had FOUR methods on both impls
// (vPJami1.(*O1OMYAe).* and vPJami1.(*TNB4quZHpWC).*) — no Tracer. Tracer is
// the new method, added with OTLP trace export.
type O11yService interface {
	// Increment increments a counter metric.
	// Binary: (*O11yServiceImpl).Increment at 0xa56720
	Increment(name string, metric *O11yMetric, tags []TagProvider)

	// RecordGauge records a gauge metric value.
	// Binary: (*O11yServiceImpl).RecordGauge at 0xa567a0
	RecordGauge(name string, metric *O11yFunctionMetric, tags []TagProvider, value float64)

	// LogHandler returns a slog.Handler for structured logging.
	// Binary: (*O11yServiceImpl).LogHandler at 0xa52a40
	LogHandler() slog.Handler

	// Tracer returns the Tracer for this service, or a singleton no-op Tracer
	// when no tracing provider is configured. NEW in Build ID 0b86a2a0.
	//
	// Binary: FKPKJ5B0zZ.(*W2z7RY).Tracer at 0x200e080 — reads the tracing
	// provider pointer at impl+0x10, returns noopTracer() (0x2015720) if nil,
	// else the cached trace.Tracer at provider+0x08/+0x10.
	// Binary: FKPKJ5B0zZ.NR2MTNm.Tracer at 0x2009740 — tail-calls noopTracer().
	Tracer() trace.Tracer

	// TODO(re): RecordLongRunningStep was listed here from the 495ea204 RE, but
	// it is NOT a method of this interface in Build ID 0b86a2a0: neither
	// implementation defines it (see the five-method set above) and the itab
	// used by FKPKJ5B0zZ.ZSY78l has only five slots. Either it was removed, or
	// it was never an interface method. Re-derive before relying on it.

	// Shutdown shuts down the observability service.
	// Binary: (*O11yServiceImpl).Shutdown at 0xa52ca0
	Shutdown(ctx context.Context) error
}

// O11yServiceImpl is the concrete implementation of O11yService,
// wrapping OpenTelemetry logger and metrics providers along with
// a DataDog statsd client.
//
// Binary: type:.eq at 0xa57fe0
// In Build ID 0b86a2a0 this type is FKPKJ5B0zZ.W2z7RY and it gained a tracing
// provider field: (*W2z7RY).Tracer at 0x200e080 dereferences impl+0x10 and, if
// non-nil, returns the trace.Tracer stored at provider+0x08/+0x10.
//
// TODO(re): the rest of the field layout below is CARRIED from the 495ea204 RE
// and has not been re-verified against 0b86a2a0; only the +0x10 tracing slot is
// established for this build.
type O11yServiceImpl struct {
	logHandler      slog.Handler
	loggingProvider *o11yLoggingProvider
	metricsProvider *o11yMetricsProvider // TODO(re): offset unverified for 0b86a2a0
	tracingProvider *o11yTracingProvider // +0x10 (Build ID 0b86a2a0)
	statsdClient    interface{}          // *statsd.Client
	ddTags          []string
}

// Tracer returns the service's Tracer, or the singleton no-op Tracer when no
// tracing provider was configured.
//
// Binary address: 0x200e080 (FKPKJ5B0zZ.(*W2z7RY).Tracer)
func (s *O11yServiceImpl) Tracer() trace.Tracer {
	if s.tracingProvider == nil {
		return noopTracer()
	}
	return s.tracingProvider.tracer
}

// alreadyWarnedAboutNoService is a flag used to warn once when GetO11yService
// is called without a configured service.
// Binary address: 0x15d1af3
var alreadyWarnedAboutNoService bool

// alreadyWarnedLock protects the warning flag.
// Binary address: 0x15d1e40
var alreadyWarnedLock sync.Mutex

// o11yServiceInstance holds the singleton O11yService.
var o11yServiceInstance O11yService

// o11yServiceLock protects the singleton.
var o11yServiceLock sync.Mutex

// LogHandler returns the slog.Handler associated with this service.
// Binary address: 0xa52a40
func (s *O11yServiceImpl) LogHandler() slog.Handler {
	return s.logHandler
}

// Increment increments a counter metric via the metrics provider and statsd.
// Binary address: 0xa56720
func (s *O11yServiceImpl) Increment(name string, metric *O11yMetric, tags []TagProvider) {
	s.metricsProvider.increment(name, metric, tags)
}

// RecordGauge records a gauge metric value via the metrics provider and statsd.
// Binary address: 0xa567a0
func (s *O11yServiceImpl) RecordGauge(name string, metric *O11yFunctionMetric, tags []TagProvider, value float64) {
	s.metricsProvider.recordGauge(name, metric, tags, value)
}

// RecordLongRunningStep records a long-running step metric via the metrics provider.
// Binary: 46 references to RecordLongRunningStep in 495ea204 binary.
//
// TODO(re): no method by this name exists in Build ID 0b86a2a0's symbol table.
// Kept as CARRIED from the 495ea204 RE, but it is not part of the current
// O11yService interface — see the note on the interface declaration.
func (s *O11yServiceImpl) RecordLongRunningStep(name string, step string, tags []TagProvider) {
	stepMetric := &O11yMetric{
		Name: name + ".long_running_step",
		Tags: []string{"step"},
	}
	stepTags := make(map[string]string)
	stepTags["step"] = step
	stepProvider := &kvTagProvider{tags: stepTags}
	allTags := append(tags, stepProvider)
	s.metricsProvider.increment(name, stepMetric, allTags)
}

// Shutdown gracefully shuts down the logging and metrics providers.
// Binary address: 0xa52ca0
func (s *O11yServiceImpl) Shutdown(ctx context.Context) error {
	var errs []error

	if s.loggingProvider != nil {
		if err := s.loggingProvider.Shutdown(ctx); err != nil {
			errs = append(errs, err)
		}
	}

	if s.metricsProvider != nil {
		if err := s.metricsProvider.Shutdown(ctx); err != nil {
			errs = append(errs, err)
		}
	}

	// Build ID 0b86a2a0 adds a third provider to shut down.
	// Binary: FKPKJ5B0zZ.(*fl_EoB).Shutdown at 0x2015420.
	// TODO(re): the ordering of the three Shutdown calls inside
	// (*W2z7RY).Shutdown (0x200e320) was not disassembled.
	if s.tracingProvider != nil {
		if err := s.tracingProvider.Shutdown(ctx); err != nil {
			errs = append(errs, err)
		}
	}

	if len(errs) > 0 {
		return errs[0]
	}
	return nil
}

// GetO11yService returns the singleton O11yService instance.
// If no service has been configured, it logs a warning once and
// returns a DiscardO11yService.
//
// Binary address: 0xa52a60
func GetO11yService(name string, tags []string) (O11yService, interface{}) {
	o11yServiceLock.Lock()
	defer o11yServiceLock.Unlock()

	if o11yServiceInstance != nil {
		return o11yServiceInstance, nil
	}

	alreadyWarnedLock.Lock()
	if !alreadyWarnedAboutNoService {
		slog.Warn("Discarding observability data. Not configured")
		alreadyWarnedAboutNoService = true
	}
	alreadyWarnedLock.Unlock()

	return DiscardO11yService{}, nil
}

// NewO11yService creates and configures a new O11yServiceImpl with
// OpenTelemetry logging and metrics, and a DataDog statsd client.
//
// Binary address: 0xa52300
// Source: service.go
func NewO11yService(ctx context.Context, cfg *O11yConfig) (O11yService, error) {
	resource, err := newResource(ctx, cfg)
	if err != nil {
		return nil, err
	}

	loggingProvider, logHandler, err := initOTelLogger(ctx, cfg, resource)
	if err != nil {
		return nil, err
	}

	metricsProvider, err := initOTelMetrics(ctx, cfg, resource)
	if err != nil {
		return nil, err
	}

	// Build ID 0b86a2a0: a third pipeline is constructed here.
	// Binary: FKPKJ5B0zZ.vKb8UqZJXTG at 0x20146a0 (see otel_traces.go).
	// TODO(re): NewO11yService's own body was not re-disassembled for
	// 0b86a2a0, so whether the tracing pipeline is built unconditionally or
	// gated on a non-empty traces endpoint is not established. The endpoint
	// tuple returned by the API client did grow by one string in this build
	// (see otel_traces.go "Endpoint plumbing"), which is why it is threaded
	// through cfg here.
	tracingProvider, err := newTracingProvider(ctx, cfg.TracesEndpoint, cfg.Headers)
	if err != nil {
		return nil, err
	}

	svc := &O11yServiceImpl{
		logHandler:      logHandler,
		loggingProvider: loggingProvider,
		metricsProvider: metricsProvider,
		tracingProvider: tracingProvider,
	}

	o11yServiceLock.Lock()
	o11yServiceInstance = svc
	o11yServiceLock.Unlock()

	return svc, nil
}

// O11yConfig holds configuration for the observability service.
//
// TODO(re): this struct is CARRIED from the 495ea204 RE and was never matched
// to a binary type. The two fields added below reflect one established fact —
// the backend client now returns THREE OTLP endpoints plus a headers map, where
// it previously returned two plus headers (see otel_traces.go "Endpoint
// plumbing") — but the names, the field order, and which returned string is the
// traces endpoint are all unrecovered.
type O11yConfig struct {
	Endpoint       string
	TracesEndpoint string            // TODO(re): name/position unrecovered
	Headers        map[string]string // TODO(re): name/position unrecovered
	APIKey         string
	Environment    string
	ServiceName    string
	DDAgentHost    string
	DDTags         []string
	Insecure       bool
}
