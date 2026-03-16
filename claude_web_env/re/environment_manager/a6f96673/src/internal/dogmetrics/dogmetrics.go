// Reconstructed stub for the dogmetrics package.
// Original import path: github.com/anthropics/anthropic/api-go/core/dogmetrics
//
// Provides a minimal DataDog metrics wrapper used by the tunnel client
// for incrementing counters (connect attempts, failures, etc.).
//
// Source: reverse-engineered from environment-manager binary (Build ID: a6f96673)

package dogmetrics

// Incr increments a named counter metric with the given key prefix and name.
// Binary: uses DataDog statsd client under the hood.
func Incr(key string, name string) {
	// TODO(re): stub — should send counter increment to DataDog agent via statsd client
}
