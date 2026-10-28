package client

import (
	"math"
	"net/http"
	"strconv"
	"time"
)

const minRetrySeconds = 60

// rateLimited reports whether a response is a GitHub rate limit: a 429, or a 403 with an exhausted primary limit
// or a Retry-After. go-github recognizes fewer shapes, and drops Retry-After from primary limits.
func rateLimited(response *http.Response) bool {
	return response.StatusCode == http.StatusTooManyRequests ||
		(response.StatusCode == http.StatusForbidden &&
			(response.Header.Get("X-RateLimit-Remaining") == "0" || len(response.Header.Values("Retry-After")) > 0))
}

// retrySeconds is the whole seconds to wait before retrying a rate-limited request. A primary limit supplies an
// epoch reset, a secondary limit may supply Retry-After as seconds or an HTTP date. Neither deadline is shortened
// by the minimum or by a maximum-delay clamp; the later of the two wins.
func retrySeconds(header http.Header, now time.Time) int {
	delay := int64(minRetrySeconds)
	retryAfter := header.Get("Retry-After")
	if seconds, ok := decimal(retryAfter); ok {
		delay = max(delay, seconds)
	} else if deadline, err := http.ParseTime(retryAfter); err == nil {
		delay = max(delay, deadline.Unix()-now.Unix())
	}
	if reset, ok := decimal(header.Get("X-RateLimit-Reset")); ok && header.Get("X-RateLimit-Remaining") == "0" {
		delay = max(delay, reset-now.Unix())
	}
	return int(min(delay, math.MaxInt32))
}

// decimal parses a non-negative ASCII decimal, saturating at MaxInt64 so that an absurd deadline stays far away.
func decimal(value string) (int64, bool) {
	if value == "" {
		return 0, false
	}
	for _, digit := range []byte(value) {
		if digit < '0' || digit > '9' {
			return 0, false
		}
	}
	// Only a range error is possible after the digit check, and ParseInt then returns the saturated value.
	parsed, _ := strconv.ParseInt(value, 10, 64)
	return parsed, true
}
