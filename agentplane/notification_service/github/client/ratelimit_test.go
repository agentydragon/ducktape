package client

import (
	"net/http"
	"testing"
	"time"
)

func TestRetrySecondsHonorsEveryDeadlineGitHubSupplies(t *testing.T) {
	now := time.Unix(1000, 250_000_000)
	for name, test := range map[string]struct {
		header map[string]string
		want   int
	}{
		"no hint":                               {nil, 60},
		"Retry-After seconds":                   {map[string]string{"Retry-After": "120"}, 120},
		"long Retry-After is not clamped":       {map[string]string{"Retry-After": "7200"}, 7200},
		"short Retry-After rises to the floor":  {map[string]string{"Retry-After": "1"}, 60},
		"Retry-After that is not a number":      {map[string]string{"Retry-After": "invalid"}, 60},
		"negative Retry-After":                  {map[string]string{"Retry-After": "-1"}, 60},
		"Retry-After beyond any clock":          {map[string]string{"Retry-After": "99999999999999999999999"}, 1<<31 - 1},
		"Retry-After as a future HTTP date":     {map[string]string{"Retry-After": "Thu, 01 Jan 1970 01:00:00 GMT"}, 2600},
		"Retry-After as a past HTTP date":       {map[string]string{"Retry-After": "Thu, 01 Jan 1970 00:00:00 GMT"}, 60},
		"exhausted primary limit":               {map[string]string{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"}, 2600},
		"reset already past":                    {map[string]string{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "999"}, 60},
		"reset that is not a number":            {map[string]string{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "invalid"}, 60},
		"reset ignored while requests remain":   {map[string]string{"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "3600"}, 60},
		"later primary reset beats Retry-After": {map[string]string{"Retry-After": "120", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"}, 2600},
		"later Retry-After beats primary reset": {map[string]string{"Retry-After": "7200", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"}, 7200},
	} {
		t.Run(name, func(t *testing.T) {
			header := http.Header{}
			for key, value := range test.header {
				header.Set(key, value)
			}
			if got := retrySeconds(header, now); got != test.want {
				t.Errorf("retrySeconds = %d, want %d", got, test.want)
			}
		})
	}
}
