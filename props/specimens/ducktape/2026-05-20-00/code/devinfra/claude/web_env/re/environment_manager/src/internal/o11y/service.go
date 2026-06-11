// Reconstructed from binary 495ea204
// Source: internal/o11y/service.go

package o11y

import (
	"context"
	"log/slog"
	"sync"
)

// O11yService is the interface for the observability service, providing
// metric increment/gauge recording, logging handler, and shutdown.
//
// Binary itab: go:itab.DiscardO11yService,O11yService at 0xf65430
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

	// RecordLongRunningStep records a long-running step for instrumentation.
	// Binary: 46 references to RecordLongRunningStep in 495ea204 binary.
	RecordLongRunningStep(name string, step string, tags []TagProvider)

	// Shutdown shuts down the observability service.
	// Binary: (*O11yServiceImpl).Shutdown at 0xa52ca0
	Shutdown(ctx context.Context) error
}

// O11yServiceImpl is the concrete implementation of O11yService,
// wrapping OpenTelemetry logger and metrics providers along with
// a DataDog statsd client.
//
// Binary: type:.eq at 0xa57fe0
type O11yServiceImpl struct {
	logHandler      slog.Handler
	loggingProvider *o11yLoggingProvider
	metricsProvider *o11yMetricsProvider
	statsdClient    interface{} // *statsd.Client
	ddTags          []string
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

	svc := &O11yServiceImpl{
		logHandler:      logHandler,
		loggingProvider: loggingProvider,
		metricsProvider: metricsProvider,
	}

	o11yServiceLock.Lock()
	o11yServiceInstance = svc
	o11yServiceLock.Unlock()

	return svc, nil
}

// O11yConfig holds configuration for the observability service.
type O11yConfig struct {
	Endpoint    string
	APIKey      string
	Environment string
	ServiceName string
	DDAgentHost string
	DDTags      []string
	Insecure    bool
}
