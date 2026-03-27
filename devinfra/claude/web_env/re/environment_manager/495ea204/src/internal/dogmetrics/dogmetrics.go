// Reconstructed from binary: environment-manager (Build ID 495ea204)
// Original import path: github.com/anthropics/anthropic/api-go/core/dogmetrics
//
// Provides a DataDog metrics wrapper used by the tunnel client
// for incrementing counters and recording distributions.
//
// Two source files exist in the original:
//   - client.go: defines the Client interface
//   - dogmetrics.go: defines Incr, Distribution, Tag, isNilClient
//
// Source: reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.

package dogmetrics

import (
	"fmt"

	"github.com/DataDog/datadog-go/v5/statsd"
)

// Client is the interface satisfied by *statsd.Client.
// The binary contains an itab entry:
//
//	go:itab.*github.com/DataDog/datadog-go/v5/statsd.Client,
//	         github.com/anthropics/anthropic/api-go/core/dogmetrics.Client
//
// The interface methods used are:
//   - Incr(name string, tags []string, rate float64) error        (vtable offset 0x48)
//   - Distribution(name string, value float64, tags []string, rate float64) error (vtable offset 0x30)
type Client interface {
	Incr(name string, tags []string, rate float64) error
	Distribution(name string, value float64, tags []string, rate float64) error
}

// compile-time check that *statsd.Client satisfies Client
var _ Client = (*statsd.Client)(nil)

// isNilClient checks if a Client interface value is nil.
//
// Binary: inlined into Incr and Distribution (not a separate symbol).
// Logic: returns true if the interface is nil (itab==nil) OR if the
// interface has the *statsd.Client itab but the data pointer is nil
// (typed nil).
//
// In the binary:
//
//	test %rax,%rax        # nil interface?
//	je   return           # yes -> nil
//	lea  statsd_itab,%rdx
//	cmp  %rdx,%rax        # is itab == *statsd.Client?
//	jne  not_nil           # different itab -> not nil
//	test %rbx,%rbx        # same itab, check data
//	je   return           # data==nil -> typed nil
func isNilClient(c Client) bool {
	return c == nil || c == Client((*statsd.Client)(nil))
}

// Incr increments a named counter metric via the DataDog statsd client.
//
// Binary address: 0xb5aee0
// Signature: Incr(client Client, name string, tags ...string)
//
// Calls client.Incr(name, tags, 1.0) with rate=1.0.
// No-op if client is nil (nil interface or typed nil *statsd.Client).
func Incr(client Client, name string, tags ...string) {
	if isNilClient(client) {
		return
	}
	client.Incr(name, tags, 1.0) //nolint:errcheck
}

// Distribution records a distribution metric via the DataDog statsd client.
//
// Binary address: 0xb5ae00
// Signature: Distribution(client Client, name string, value float64, tags ...string)
//
// Calls client.Distribution(name, value, tags, 1.0) with rate=1.0.
// No-op if client is nil (nil interface or typed nil *statsd.Client).
func Distribution(client Client, name string, value float64, tags ...string) {
	if isNilClient(client) {
		return
	}
	client.Distribution(name, value, tags, 1.0) //nolint:errcheck
}

// Tag formats a DataDog tag as "key:value".
//
// Binary: inlined (no separate symbol in nm output, but present in strings
// as dogmetrics.Tag). Equivalent to fmt.Sprintf("%s:%s", key, value).
func Tag(key, value string) string {
	return fmt.Sprintf("%s:%s", key, value)
}
