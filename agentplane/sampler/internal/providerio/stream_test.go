package providerio

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

func TestPostAcceptsOnly2xxAndBoundsErrorBodies(t *testing.T) {
	for _, tc := range []struct {
		status          int
		body            string
		ok, transient   bool
		wantErrorSuffix string
	}{
		{status: http.StatusOK, ok: true},
		{status: http.StatusNoContent, ok: true},
		{status: http.StatusNotModified},
		{status: http.StatusBadRequest, body: strings.Repeat("x", maxErrorBodyBytes+1), wantErrorSuffix: "x[truncated]"},
		{status: http.StatusTooManyRequests, transient: true},
	} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
			if request.Header.Get("x-layer") != "override" || request.Header.Get("Authorization") != "" {
				t.Errorf("headers=%v, want the later layer to override and an empty value to unset", request.Header)
			}
			w.Header().Set("x-request-id", "fixture-request")
			w.WriteHeader(tc.status)
			_, _ = w.Write([]byte(tc.body))
		}))
		headRecorded := false
		response, err := Post(t.Context(), server.Client(), Request{
			URL: server.URL, Provider: "fixture", RequestIDHeader: "x-request-id",
			Headers: []map[string]string{{"x-layer": "base", "Authorization": "fixture"}, {"x-layer": "override", "Authorization": ""}},
		}, nil, protocol.ProviderHooks{ResponseHeadRecorded: func() { headRecorded = true }})
		server.Close()
		if !headRecorded {
			t.Errorf("status %d: response head hook not fired", tc.status)
		}
		if tc.ok {
			if err != nil || response.Head.ProviderRequestID != "fixture-request" {
				t.Errorf("status %d: response=%+v err=%v", tc.status, response, err)
			} else {
				response.Close()
			}
			continue
		}
		var transient *protocol.ProviderTransientError
		var known *protocol.KnownFailureError
		switch {
		case tc.transient && !errors.As(err, &transient):
			t.Errorf("status %d: err=%v, want transient", tc.status, err)
		case !tc.transient && !errors.As(err, &known):
			t.Errorf("status %d: err=%v, want known failure", tc.status, err)
		case known != nil && !strings.HasSuffix(known.Evidence["body"].(string), tc.wantErrorSuffix):
			t.Errorf("status %d: error body not bounded", tc.status)
		}
	}
}

func TestReadSSEFramesDataLines(t *testing.T) {
	stream := "event: x\ndata: first\r\ndata:\tsecond\n\n: comment\n\ndata:  spaced\n\ndata: unterminated"
	var events []string
	if err := ReadSSE(t.Context(), strings.NewReader(stream), func(data string) error {
		events = append(events, data)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	// One leading space is dropped; a tab or a second space is data. An event without its blank line never dispatches.
	if want := []string{"first\n\tsecond", " spaced"}; !slices.Equal(events, want) {
		t.Fatalf("events=%q, want %q", events, want)
	}
	stop := errors.New("fixture stop")
	if err := ReadSSE(t.Context(), strings.NewReader("data: a\n\ndata: b\n\n"), func(string) error { return stop }); !errors.Is(err, stop) {
		t.Fatalf("dispatch error=%v, want it returned", err)
	}
}
