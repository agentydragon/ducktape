package observability

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"runtime"
	"strings"
	"sync"

	"github.com/google/uuid"
	"github.com/prometheus/procfs"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
)

// newMeterProvider is opt-in so existing telemetry-enabled services continue exporting only
// traces until their deployment explicitly enables metrics.
func newMeterProvider(ctx context.Context, baseResource *resource.Resource) (*sdkmetric.MeterProvider, error) {
	exporterNames, enabled := os.LookupEnv("OTEL_METRICS_EXPORTER")
	if !enabled || strings.TrimSpace(exporterNames) == "" {
		return nil, nil
	}
	if strings.EqualFold(strings.TrimSpace(exporterNames), "none") {
		return nil, nil
	}
	if !strings.EqualFold(strings.TrimSpace(exporterNames), "otlp") {
		return nil, fmt.Errorf("unsupported OTEL_METRICS_EXPORTER value %q", strings.TrimSpace(exporterNames))
	}
	if !hasOTLPMetricsEndpoint() {
		return nil, errors.New("OTEL_METRICS_EXPORTER=otlp requires OTEL_EXPORTER_OTLP_METRICS_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT")
	}
	protocol := os.Getenv("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL")
	if protocol == "" {
		protocol = os.Getenv("OTEL_EXPORTER_OTLP_PROTOCOL")
	}
	if protocol != "" && !strings.EqualFold(strings.TrimSpace(protocol), "grpc") {
		return nil, fmt.Errorf("OTLP metrics exporter requires gRPC, got protocol %q", strings.TrimSpace(protocol))
	}
	preference := os.Getenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE")
	if preference != "" && !strings.EqualFold(strings.TrimSpace(preference), "cumulative") {
		return nil, fmt.Errorf("OTLP metrics exporter requires cumulative temporality, got preference %q", strings.TrimSpace(preference))
	}

	instanceResource := resource.NewWithAttributes(
		baseResource.SchemaURL(),
		attribute.String("service.instance.id", uuid.NewString()),
	)
	if _, exists := baseResource.Set().Value(attribute.Key("service.instance.id")); exists {
		instanceResource = resource.Empty()
	}
	metricResource, err := resource.Merge(baseResource, instanceResource)
	if err != nil {
		return nil, fmt.Errorf("create metric resource: %w", err)
	}

	exporter, err := otlpmetricgrpc.New(
		ctx,
		otlpmetricgrpc.WithTemporalitySelector(sdkmetric.CumulativeTemporalitySelector),
	)
	if err != nil {
		return nil, fmt.Errorf("create OTLP metric exporter: %w", err)
	}
	provider := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(metricResource),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter)),
		sdkmetric.WithView(sdkmetric.NewView(
			sdkmetric.Instrument{Name: "agentplane_mcp_oauth_token_request_duration_seconds", Kind: sdkmetric.InstrumentKindHistogram},
			sdkmetric.Stream{Aggregation: sdkmetric.AggregationExplicitBucketHistogram{
				Boundaries: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10},
			}},
		)),
	)
	if err := registerRuntimeAndProcessMetrics(provider.Meter("github.com/agentydragon/ducktape/agentplane/observability")); err != nil {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		_ = provider.Shutdown(shutdownCtx)
		return nil, err
	}
	return provider, nil
}

func hasOTLPMetricsEndpoint() bool {
	return strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")) != "" ||
		strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")) != ""
}

func registerRuntimeAndProcessMetrics(meter metric.Meter) error {
	goRoutines, err := meter.Int64ObservableGauge("runtime.go.goroutines",
		metric.WithDescription("Number of goroutines currently tracked by the Go runtime."), metric.WithUnit("{goroutine}"))
	if err != nil {
		return fmt.Errorf("create Go goroutine metric: %w", err)
	}
	heapAlloc, err := meter.Int64ObservableGauge("runtime.go.mem.heap_alloc",
		metric.WithDescription("Bytes of allocated heap objects still in use."), metric.WithUnit("By"))
	if err != nil {
		return fmt.Errorf("create Go heap allocation metric: %w", err)
	}
	heapInUse, err := meter.Int64ObservableGauge("runtime.go.mem.heap_inuse",
		metric.WithDescription("Bytes in in-use heap spans."), metric.WithUnit("By"))
	if err != nil {
		return fmt.Errorf("create Go heap in-use metric: %w", err)
	}
	heapSys, err := meter.Int64ObservableGauge("runtime.go.mem.heap_sys",
		metric.WithDescription("Bytes of virtual address space reserved for the heap."), metric.WithUnit("By"))
	if err != nil {
		return fmt.Errorf("create Go heap system metric: %w", err)
	}
	gcCount, err := meter.Int64ObservableCounter("runtime.go.gc.count",
		metric.WithDescription("Number of completed Go garbage collection cycles since process start."), metric.WithUnit("{collection}"))
	if err != nil {
		return fmt.Errorf("create Go garbage collection count metric: %w", err)
	}
	gcPause, err := meter.Float64ObservableCounter("runtime.go.gc.pause_total",
		metric.WithDescription("Cumulative duration of Go garbage collection pauses."), metric.WithUnit("s"))
	if err != nil {
		return fmt.Errorf("create Go garbage collection pause metric: %w", err)
	}
	processCPU, err := meter.Float64ObservableCounter("process.cpu.time",
		metric.WithDescription("Cumulative user and system CPU time consumed by this process."), metric.WithUnit("s"))
	if err != nil {
		return fmt.Errorf("create process CPU time metric: %w", err)
	}
	processRSS, err := meter.Int64ObservableGauge("process.memory.rss",
		metric.WithDescription("Resident memory size of this process."), metric.WithUnit("By"))
	if err != nil {
		return fmt.Errorf("create process RSS metric: %w", err)
	}
	processFDs, err := meter.Int64ObservableGauge("process.open_file_descriptors",
		metric.WithDescription("Number of file descriptors currently open by this process."), metric.WithUnit("{file_descriptor}"))
	if err != nil {
		return fmt.Errorf("create process file descriptor metric: %w", err)
	}

	var procfsWarning sync.Once
	_, err = meter.RegisterCallback(func(ctx context.Context, observer metric.Observer) error {
		observer.ObserveInt64(goRoutines, int64(runtime.NumGoroutine()))
		var stats runtime.MemStats
		runtime.ReadMemStats(&stats)
		observer.ObserveInt64(heapAlloc, int64(stats.HeapAlloc))
		observer.ObserveInt64(heapInUse, int64(stats.HeapInuse))
		observer.ObserveInt64(heapSys, int64(stats.HeapSys))
		observer.ObserveInt64(gcCount, int64(stats.NumGC))
		observer.ObserveFloat64(gcPause, float64(stats.PauseTotalNs)/1e9)

		process, processErr := procfs.Self()
		if processErr != nil {
			procfsWarning.Do(func() { slog.Warn("cannot read process metrics from procfs", "error", processErr) })
			return nil
		}
		stat, processErr := process.Stat()
		if processErr != nil {
			procfsWarning.Do(func() { slog.Warn("cannot read process CPU time from procfs", "error", processErr) })
			return nil
		}
		observer.ObserveFloat64(processCPU, stat.CPUTime())
		observer.ObserveInt64(processRSS, int64(stat.ResidentMemory()))
		fds, processErr := process.FileDescriptorsLen()
		if processErr != nil {
			procfsWarning.Do(func() { slog.Warn("cannot read process file descriptor count from procfs", "error", processErr) })
			return nil
		}
		observer.ObserveInt64(processFDs, int64(fds))
		return nil
	}, goRoutines, heapAlloc, heapInUse, heapSys, gcCount, gcPause, processCPU, processRSS, processFDs)
	if err != nil {
		return fmt.Errorf("register Go runtime and process metrics: %w", err)
	}
	return nil
}
