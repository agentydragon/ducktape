package linkage

import (
	"context"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

func TestObserveRecordsOAuthTokenRequestHistogram(t *testing.T) {
	reader := metric.NewManualReader()
	provider := metric.NewMeterProvider(metric.WithReader(reader))
	otel.SetMeterProvider(provider)
	t.Cleanup(func() {
		if err := provider.Shutdown(context.Background()); err != nil {
			t.Errorf("shutdown meter provider: %v", err)
		}
	})

	observe("exchange", "rejected", time.Now().Add(-250*time.Millisecond))

	var collected metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &collected); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	for _, scope := range collected.ScopeMetrics {
		for _, item := range scope.Metrics {
			if item.Name != tokenRequestDurationMetricName {
				continue
			}
			if item.Unit != "s" {
				t.Errorf("OAuth token request unit = %q, want seconds", item.Unit)
			}
			histogram, ok := item.Data.(metricdata.Histogram[float64])
			if !ok {
				t.Fatalf("OAuth token request data type = %T, want histogram", item.Data)
			}
			if len(histogram.DataPoints) != 1 {
				t.Fatalf("OAuth token request data points = %d, want 1", len(histogram.DataPoints))
			}
			dataPoint := histogram.DataPoints[0]
			if dataPoint.Count != 1 || dataPoint.Sum < 0.2 {
				t.Errorf("OAuth token request histogram count/sum = %d/%.3f, want one observation in seconds", dataPoint.Count, dataPoint.Sum)
			}
			for key, want := range map[string]string{"operation": "exchange", "outcome": "rejected"} {
				value, exists := dataPoint.Attributes.Value(attribute.Key(key))
				if !exists || value.AsString() != want {
					t.Errorf("OAuth token request %q attribute = %q (present=%v), want %q", key, value.AsString(), exists, want)
				}
			}
			return
		}
	}
	t.Fatalf("OTel metric %q was not recorded by observe", tokenRequestDurationMetricName)
}
