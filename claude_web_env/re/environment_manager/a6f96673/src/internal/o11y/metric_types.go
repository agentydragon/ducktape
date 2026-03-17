// Reconstructed from binary a6f96673
// Source: internal/o11y/metric_types.go

package o11y

import (
	"net/http"
)

// O11yMetric defines a named metric with tags.
// Binary: type:.eq at 0xa57440
type O11yMetric struct {
	Name string
	Tags []string
}

// O11yFunctionMetric defines a metric for function-level instrumentation
// with increment and distribution sub-metric names and a prefix.
// Binary: type:.eq at 0xa58060
type O11yFunctionMetric struct {
	Prefix           string
	IncrementName    string
	DistributionName string
	Tags             []string
	Unit             string // e.g., "ms"
}

// TagProvider is an interface for objects that provide metric tags.
// Binary itab: go:itab.*kvTagProvider,TagProvider at 0xf5a3c0
type TagProvider interface {
	Tags() map[string]string
}

// kvTagProvider wraps a map of tags and implements TagProvider.
// Binary: (*kvTagProvider).Tags at 0xa516e0
type kvTagProvider struct {
	tags map[string]string
}

// Tags returns the underlying tag map.
// Binary address: 0xa516e0
func (p *kvTagProvider) Tags() map[string]string {
	return p.tags
}

// ErrorTags returns a TagProvider with "error_type" and "error_message" tags
// extracted from the given error. If err is nil, returns a TagProvider with
// an empty map. If the error implements an interface with StatusCode() int,
// the error_type is set to "code" and error_message to the status code string;
// otherwise error_type is "unknown" and error_message is the error string.
//
// Binary address: 0xa51700
// Source: metric_types.go
func ErrorTags(err error, _ interface{}) (TagProvider, interface{}) {
	if err == nil {
		tags := make(map[string]string)
		return &kvTagProvider{tags: tags}, nil
	}

	var statusErr interface {
		StatusCode() int
	}
	if asErr, ok := err.(interface{ StatusCode() int }); ok {
		_ = asErr
		tags := make(map[string]string)
		tags["error_type"] = "code"
		code := statusErr.StatusCode()
		tags["error_message"] = http.StatusText(code)
		return &kvTagProvider{tags: tags}, nil
	}

	tags := make(map[string]string)
	tags["error_message"] = "unknown"
	tags["error_type"] = "true"
	return &kvTagProvider{tags: tags}, nil
}

// mergeTags merges multiple TagProvider tag maps into a single map.
// Binary address: 0xa519a0
func mergeTags(providers []TagProvider) map[string]string {
	merged := make(map[string]string)
	for _, p := range providers {
		if p == nil {
			continue
		}
		for k, v := range p.Tags() {
			merged[k] = v
		}
	}
	return merged
}
