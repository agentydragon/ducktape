package liveinventory

import (
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"testing"
	"time"
)

type testClock struct{ current time.Time }

func (c *testClock) Now() time.Time { return c.current }

type testBody struct {
	clock *testClock
	after time.Duration
	err   error
}

func (b testBody) Read([]byte) (int, error) {
	b.clock.current = b.clock.current.Add(b.after)
	return 0, b.err
}

func (testBody) Close() error { return nil }

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) { return f(request) }

func TestOnlyFullCleanWatchEndAdvancesFreshness(t *testing.T) {
	watchURL := "http://api.test.invalid/apis/agentplane.allegedly.works/v1alpha1/namespaces/n/policysets?watch=true"
	for name, test := range map[string]struct {
		method       string
		watch        bool
		hasPriorList bool
		after        time.Duration
		endError     error
		status       int
		cycle        bool
		listRunCount uint64
	}{
		"full clean watch after list": {
			method: http.MethodGet, watch: true, hasPriorList: true, after: 2 * time.Second, endError: io.EOF, status: http.StatusOK, cycle: true,
		},
		"full clean watch without list": {
			method: http.MethodGet, watch: true, after: 2 * time.Second, endError: io.EOF, status: http.StatusOK,
		},
		"early clean end": {
			method: http.MethodGet, watch: true, hasPriorList: true, after: time.Second, endError: io.EOF, status: http.StatusOK,
		},
		"connection failure": {
			method: http.MethodGet, watch: true, hasPriorList: true, after: 2 * time.Second, endError: io.ErrUnexpectedEOF, status: http.StatusOK,
		},
		"successful list": {
			method: http.MethodGet, watch: false, after: 2 * time.Second, endError: io.EOF, status: http.StatusOK, listRunCount: 1,
		},
		"failed list": {
			method: http.MethodGet, watch: false, after: 2 * time.Second, endError: io.EOF, status: http.StatusServiceUnavailable,
		},
		"other method": {
			method: http.MethodPost, watch: true, after: 2 * time.Second, endError: io.EOF, status: http.StatusOK,
		},
	} {
		t.Run(name, func(t *testing.T) {
			clock := &testClock{current: time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)}
			index := &Index{
				period: 2 * time.Second, now: clock.Now, cycles: map[string]time.Time{},
				listRuns: map[string]uint64{}, cycleRuns: map[string]uint64{},
			}
			if test.hasPriorList {
				index.recordList(PolicySetsKind)
			}
			var observedTimeout int64
			transport := cycleRecorder{index: index}.wrap(roundTripFunc(func(request *http.Request) (*http.Response, error) {
				observedTimeout, _ = strconv.ParseInt(request.URL.Query().Get("timeoutSeconds"), 10, 64)
				return &http.Response{
					StatusCode: test.status, Body: testBody{clock: clock, after: test.after, err: test.endError}, Request: request,
				}, nil
			}))
			request, err := http.NewRequest(test.method, watchURL, nil)
			if err != nil {
				t.Fatal(err)
			}
			if !test.watch {
				query := url.Values{"watch": []string{"false"}}
				request.URL.RawQuery = query.Encode()
			}
			response, err := transport.RoundTrip(request)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := response.Body.Read(nil); !errors.Is(err, test.endError) {
				t.Fatalf("stream ended with %v, want %v", err, test.endError)
			}
			if got := index.cycleRuns[PolicySetsKind]; test.cycle != (got > 0) {
				t.Fatalf("cycle recorded = %v, want %v", got > 0, test.cycle)
			}
			if got := index.listRuns[PolicySetsKind]; got != test.listRunCount+uint64FromBool(test.hasPriorList) {
				t.Fatalf("successful list runs = %d, want %d", got, test.listRunCount+uint64FromBool(test.hasPriorList))
			}
			if test.watch && test.method == http.MethodGet && observedTimeout != 2 {
				t.Fatalf("watch timeoutSeconds = %d, want 2", observedTimeout)
			}
		})
	}
}

func TestConsecutiveCyclesAdvanceFreshnessWithinAListRun(t *testing.T) {
	first := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	index := &Index{
		cycles:    map[string]time.Time{},
		listRuns:  map[string]uint64{},
		cycleRuns: map[string]uint64{},
	}
	index.recordCycle(PolicySetsKind, index.listRun(PolicySetsKind), first)
	if _, ok := index.cycles[PolicySetsKind]; ok {
		t.Fatal("cycle before any list recorded freshness")
	}
	firstListRun := index.recordList(PolicySetsKind)
	index.recordCycle(PolicySetsKind, firstListRun, first)
	second := first.Add(5 * time.Minute)
	index.recordCycle(PolicySetsKind, firstListRun, second)
	if got := index.cycles[PolicySetsKind]; !got.Equal(second) {
		t.Fatalf("second cycle of the same list run left freshness at %s, want %s", got, second)
	}
	secondListRun := index.recordList(PolicySetsKind)
	third := second.Add(5 * time.Minute)
	index.recordCycle(PolicySetsKind, secondListRun, third)
	index.recordCycle(PolicySetsKind, firstListRun, third.Add(time.Second))
	if got := index.cycles[PolicySetsKind]; !got.Equal(third) {
		t.Fatalf("cycle from a superseded list run moved freshness to %s, want %s", got, third)
	}
}

func uint64FromBool(value bool) uint64 {
	if value {
		return 1
	}
	return 0
}
