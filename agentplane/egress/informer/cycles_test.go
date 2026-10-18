package informer

import (
	"errors"
	"io"
	"net/http"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/policy"
)

type fakeClock struct{ now time.Time }

func (c *fakeClock) Now() time.Time { return c.now }

// endingBody is a watch stream that ends with err once the clock has run for after.
type endingBody struct {
	clock *fakeClock
	after time.Duration
	err   error
}

func (b endingBody) Read([]byte) (int, error) {
	b.clock.now = b.clock.now.Add(b.after)
	return 0, b.err
}

func (endingBody) Close() error { return nil }

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) { return f(request) }

func TestOnlyAWatchTheServerEndsOnScheduleIsACycle(t *testing.T) {
	const watch = "http://api.test.invalid/apis/g/v/namespaces/n/policysets?watch=true&timeoutSeconds=60"
	for name, tc := range map[string]struct {
		url    string
		after  time.Duration
		err    error
		cycles bool
	}{
		"ended by the server's timeout":   {watch, time.Minute, io.EOF, true},
		"ended early":                     {watch, 30 * time.Second, io.EOF, false},
		"connection lost at the period":   {watch, time.Minute, io.ErrUnexpectedEOF, false},
		"watch with no period":            {"http://api.test.invalid/apis/g/v/namespaces/n/policysets?watch=true", time.Minute, io.EOF, false},
		"list that happens to take a bit": {"http://api.test.invalid/apis/g/v/namespaces/n/policysets?timeoutSeconds=60", time.Minute, io.EOF, false},
	} {
		t.Run(name, func(t *testing.T) {
			clock := &fakeClock{now: time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)}
			index := policy.NewIndex(time.Hour)
			transport := cycleRecorder{index: index, now: clock.Now}.wrap(roundTripFunc(func(request *http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: http.StatusOK, Body: endingBody{clock, tc.after, tc.err}, Request: request}, nil
			}))
			request, err := http.NewRequest(http.MethodGet, tc.url, nil)
			if err != nil {
				t.Fatal(err)
			}
			response, err := transport.RoundTrip(request)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := response.Body.Read(nil); !errors.Is(err, tc.err) {
				t.Fatalf("the stream ended with %v, want %v", err, tc.err)
			}
			if _, recorded := index.Status(clock.now).Ages[policy.PolicySets]; recorded != tc.cycles {
				t.Errorf("a cycle of policysets was recorded = %v, want %v", recorded, tc.cycles)
			}
		})
	}
}
