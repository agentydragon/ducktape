// Reconstructed from binary 6b49f1ca
// Source: internal/o11y/otel_metrics.go
//
// This file contains the OpenTelemetry metrics provider, counter/gauge
// instrument management, the OTLP/stdout exporter construction, and
// the RecordFunctionDeferred helper.

package o11y

import (
	"context"
	"fmt"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/stdout/stdoutmetric"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/resource"
)

// o11yMetricsProvider wraps an OTel MeterProvider and caches created instruments.
//
// Binary: (*o11yMetricsProvider).Shutdown at 0xa54940
// Binary: (*o11yMetricsProvider).getOrCreateCounter at 0xa55620
// Binary: (*o11yMetricsProvider).getOrCreateGauge at 0xa55840
// Binary: (*o11yMetricsProvider).increment at 0xa55a60
// Binary: (*o11yMetricsProvider).recordGauge at 0xa55f60
type o11yMetricsProvider struct {
	provider *sdkmetric.MeterProvider
	mu       sync.Mutex
	counters map[string]metric.Int64Counter
	gauges   map[string]metric.Float64Gauge
}

// Shutdown shuts down the underlying MeterProvider.
// Binary address: 0xa54940
func (p *o11yMetricsProvider) Shutdown(ctx context.Context) error {
	if p.provider != nil {
		return p.provider.Shutdown(ctx)
	}
	return nil
}

// getOrCreateCounter returns an existing Int64Counter or creates a new one.
// Binary address: 0xa55620
// Binary: getOrCreateInstrument[Int64Counter] at 0xa57940
func (p *o11yMetricsProvider) getOrCreateCounter(name string) (metric.Int64Counter, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	if c, ok := p.counters[name]; ok {
		return c, nil
	}

	meter := p.provider.Meter("environment-runner")
	c, err := meter.Int64Counter(name)
	if err != nil {
		return nil, err
	}
	p.counters[name] = c
	return c, nil
}

// getOrCreateGauge returns an existing Float64Gauge or creates a new one.
// Binary address: 0xa55840
// Binary: getOrCreateInstrument[Float64Gauge] at 0xa57500
func (p *o11yMetricsProvider) getOrCreateGauge(name string) (metric.Float64Gauge, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	if g, ok := p.gauges[name]; ok {
		return g, nil
	}

	meter := p.provider.Meter("environment-runner")
	g, err := meter.Float64Gauge(name)
	if err != nil {
		return nil, err
	}
	p.gauges[name] = g
	return g, nil
}

// increment increments a counter metric by 1, merging the provided tags
// into OTel attributes.
// Binary address: 0xa55a60
func (p *o11yMetricsProvider) increment(name string, m *O11yMetric, tags []TagProvider) {
	counter, err := p.getOrCreateCounter(m.Name)
	if err != nil {
		return
	}

	merged := mergeTags(tags)
	attrs := tagsToAttributes(merged)
	counter.Add(context.Background(), 1, metric.WithAttributes(attrs...))
}

// recordGauge records a gauge metric value, merging provided tags
// into OTel attributes.
// Binary address: 0xa55f60
func (p *o11yMetricsProvider) recordGauge(name string, m *O11yFunctionMetric, tags []TagProvider, value float64) {
	gauge, err := p.getOrCreateGauge(m.IncrementName)
	if err != nil {
		return
	}

	merged := mergeTags(tags)
	attrs := tagsToAttributes(merged)
	gauge.Record(context.Background(), value, metric.WithAttributes(attrs...))
}

// tagsToAttributes converts a map of string tags to OTel attributes.
func tagsToAttributes(tags map[string]string) []attribute.KeyValue {
	attrs := make([]attribute.KeyValue, 0, len(tags))
	for k, v := range tags {
		attrs = append(attrs, attribute.String(k, v))
	}
	return attrs
}

// initOTelMetrics initializes the OpenTelemetry metrics pipeline with
// a periodic reader and the appropriate exporter (OTLP HTTP or stdout).
//
// Binary address: 0xa54100
// Source: otel_metrics.go
func initOTelMetrics(ctx context.Context, cfg *O11yConfig, res *resource.Resource) (*o11yMetricsProvider, error) {
	var exporter sdkmetric.Exporter
	var err error

	if cfg.Endpoint != "" {
		exporter, err = makeOTLPExporter(cfg)
	} else {
		exporter, err = makeStdoutExporter()
	}
	if err != nil {
		return nil, err
	}

	reader := sdkmetric.NewPeriodicReader(exporter,
		sdkmetric.WithInterval(60*time.Second),
		sdkmetric.WithTimeout(30*time.Second),
	)

	provider := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(reader),
		sdkmetric.WithResource(res),
	)

	return &o11yMetricsProvider{
		provider: provider,
		counters: make(map[string]metric.Int64Counter),
		gauges:   make(map[string]metric.Float64Gauge),
	}, nil
}

// makeOTLPExporter creates an OTLP HTTP metric exporter with the configured
// endpoint, API key header, and optional insecure transport.
//
// Binary address: 0xa54aa0
// Source: otel_metrics.go
func makeOTLPExporter(cfg *O11yConfig) (sdkmetric.Exporter, error) {
	endpoint := makeOTLPEndpoint(cfg.Endpoint, cfg.APIKey, "metrics")

	headers := map[string]string{
		"dd-api-key":  cfg.Environment + cfg.APIKey,
		"dd-protocol": cfg.Environment,
	}

	opts := []otlpmetrichttp.Option{
		otlpmetrichttp.WithEndpointURL(endpoint),
		otlpmetrichttp.WithHeaders(headers),
	}

	if cfg.Endpoint == "http://" {
		opts = append(opts, otlpmetrichttp.WithInsecure())
	}

	opts = append(opts, otlpmetrichttp.WithTemporalitySelector(
		func(kind sdkmetric.InstrumentKind) metricdata.Temporality {
			return metricdata.DeltaTemporality
		},
	))

	exporter, err := otlpmetrichttp.New(context.Background(), opts...)
	if err != nil {
		return nil, err
	}
	return exporter, nil
}

// makeStdoutExporter creates a stdout metric exporter with delta temporality.
//
// Binary address: 0xa55580
// Source: otel_metrics.go
func makeStdoutExporter() (sdkmetric.Exporter, error) {
	exporter, err := stdoutmetric.New(
		stdoutmetric.WithTemporalitySelector(func(kind sdkmetric.InstrumentKind) metricdata.Temporality {
			return metricdata.DeltaTemporality
		}),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create stdout metric exporter: %w", err)
	}
	return exporter, nil
}

// Increment is the package-level convenience function that obtains the
// singleton O11yService and calls its Increment method.
//
// Binary address: 0xa56820
func Increment(ctx context.Context, metric *O11yMetric, providers []TagProvider) {
	svc, _ := GetO11yService("", nil)
	svc.Increment("", metric, providers)
}

// RecordGauge is the package-level convenience function that obtains the
// singleton O11yService and calls its RecordGauge method.
//
// Binary address: 0xa56920
func RecordGauge(name string, tags []string, metric *O11yFunctionMetric, providers []TagProvider, value float64) {
	svc, _ := GetO11yService(name, tags)
	svc.RecordGauge(name, metric, providers, value)
}

// RecordFunctionDeferred returns a function that, when called (typically via
// defer), records the elapsed time since startTime as a distribution metric
// and increments the count metric. It also appends error tags.
//
// Binary address: 0xa56a40
// Source: otel_metrics.go
func RecordFunctionDeferred(
	name string,
	tags []string,
	fm *O11yFunctionMetric,
	startTime time.Time,
	extraTags []TagProvider,
) func(error, interface{}) {
	// RecordFunction is constructed inline; see func2 at 0xa56c20
	return func(err error, errItf interface{}) {
		// Binary address: 0xa56c20 (RecordFunctionDeferred.RecordFunction.func2)
		durationMs := time.Since(startTime).Milliseconds()

		// Build tags slice: copy extraTags + errorTags
		allTags := make([]TagProvider, len(extraTags))
		copy(allTags, extraTags)

		errorProvider, _ := ErrorTags(err, errItf)
		allTags = append(allTags, errorProvider)

		merged := mergeTags(allTags)
		_ = merged

		// Build increment metric name: prefix + "count." + name
		incrMetric := &O11yFunctionMetric{
			IncrementName:    fm.Prefix + "count." + fm.IncrementName,
			DistributionName: fm.Prefix + "duration." + fm.DistributionName,
			Unit:             "ms",
		}
		_ = incrMetric

		// Compute float64 duration
		durationFloat := float64(durationMs)

		// Get service and record both increment and gauge
		svc, _ := GetO11yService(name, tags)
		svc.Increment(name, nil, allTags)
		svc.RecordGauge(name, incrMetric, allTags, durationFloat)
	}
}
