package policies

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// gitHubStub answers every lookup with status and body and counts the lookups.
type gitHubStub struct {
	t      *testing.T
	status int
	body   string
	calls  atomic.Int32
}

func (s *gitHubStub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.calls.Add(1)
	if s.status == http.StatusFound {
		http.Redirect(w, r, "/repos/test-owner/test-public", http.StatusFound)
		return
	}
	w.WriteHeader(s.status)
	if _, err := w.Write([]byte(s.body)); err != nil {
		s.t.Error(err)
	}
}

func serve(t *testing.T, handler http.Handler, ttl time.Duration) *Visibility {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	visibility, err := NewVisibility(server.URL, ttl)
	if err != nil {
		t.Fatal(err)
	}
	return visibility
}

func TestGitHubsAnswersMapToVisibility(t *testing.T) {
	for name, test := range map[string]struct {
		status int
		body   string
		public bool
	}{
		"public":       {status: http.StatusOK, body: `{"private": false}`, public: true},
		"not found":    {status: http.StatusNotFound, public: false},
		"private":      {status: http.StatusOK, body: `{"private": true}`},
		"no private":   {status: http.StatusOK, body: `{}`},
		"private null": {status: http.StatusOK, body: `{"private": null}`},
		"not JSON":     {status: http.StatusOK, body: `<html>`},
		"rate limited": {status: http.StatusForbidden},
		"server error": {status: http.StatusInternalServerError},
		"bad gateway":  {status: http.StatusBadGateway},
		"redirected":   {status: http.StatusFound},
	} {
		t.Run(name, func(t *testing.T) {
			stub := &gitHubStub{t: t, status: test.status, body: test.body}
			public, err := serve(t, stub, VisibilityTTL).IsPublic(t.Context(), "test-owner", "test-repo")
			wantUnavailable := test.status != http.StatusNotFound && !test.public
			if wantUnavailable != errors.Is(err, ErrVisibilityUnavailable) || public != test.public {
				t.Errorf("public %v, error %v", public, err)
			}
			if calls := stub.calls.Load(); calls != 1 {
				t.Errorf("%d lookups; a redirect is not followed", calls)
			}
		})
	}
}

func TestAnUnreachableGitHubIsUnavailable(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	server.Close()
	visibility, err := NewVisibility(server.URL, VisibilityTTL)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := visibility.IsPublic(t.Context(), "test-owner", "test-repo"); !errors.Is(err, ErrVisibilityUnavailable) {
		t.Errorf("error %v", err)
	}
}

func TestAnAnswerIsReusedWithinTheTTL(t *testing.T) {
	for name, status := range map[string]int{"public": http.StatusOK, "not public": http.StatusNotFound} {
		t.Run(name, func(t *testing.T) {
			stub := &gitHubStub{t: t, status: status, body: `{"private": false}`}
			visibility := serve(t, stub, VisibilityTTL)
			first, firstErr := visibility.IsPublic(t.Context(), "test-owner", "test-repo")
			second, secondErr := visibility.IsPublic(t.Context(), "test-owner", "test-repo")
			if firstErr != nil || secondErr != nil || first != second || stub.calls.Load() != 1 {
				t.Errorf("%v %v then %v %v after %d lookups", first, firstErr, second, secondErr, stub.calls.Load())
			}
		})
	}
}

func TestNamesShareAnAnswerAcrossCase(t *testing.T) {
	stub := &gitHubStub{t: t, status: http.StatusOK, body: `{"private": false}`}
	visibility := serve(t, stub, VisibilityTTL)
	for _, pair := range [][2]string{{"Test-Owner", "STRASSE"}, {"test-owner", "straße"}} {
		if public, err := visibility.IsPublic(t.Context(), pair[0], pair[1]); !public || err != nil {
			t.Fatalf("%v: %v %v", pair, public, err)
		}
	}
	if calls := stub.calls.Load(); calls != 1 {
		t.Errorf("%d lookups", calls)
	}
}

func TestAZeroTTLLooksUpAgain(t *testing.T) {
	stub := &gitHubStub{t: t, status: http.StatusOK, body: `{"private": false}`}
	visibility := serve(t, stub, 0)
	for range 2 {
		if _, err := visibility.IsPublic(t.Context(), "test-owner", "test-repo"); err != nil {
			t.Fatal(err)
		}
	}
	if calls := stub.calls.Load(); calls != 2 {
		t.Errorf("%d lookups", calls)
	}
}

func TestAFailedLookupIsNotReused(t *testing.T) {
	stub := &gitHubStub{t: t, status: http.StatusInternalServerError}
	visibility := serve(t, stub, VisibilityTTL)
	for range 2 {
		if _, err := visibility.IsPublic(t.Context(), "test-owner", "test-repo"); !errors.Is(err, ErrVisibilityUnavailable) {
			t.Fatalf("error %v", err)
		}
	}
	if calls := stub.calls.Load(); calls != 2 {
		t.Errorf("%d lookups", calls)
	}
}

func TestACallerGivingUpLeavesTheLookupToTheOthers(t *testing.T) {
	// Room for a second lookup, so one would be counted rather than wedge.
	arrived, release := make(chan struct{}, 2), make(chan struct{})
	var calls atomic.Int32
	visibility := serve(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		arrived <- struct{}{}
		<-release
		if _, err := w.Write([]byte(`{"private": false}`)); err != nil {
			t.Error(err)
		}
	}), VisibilityTTL)

	ctx, cancel := context.WithCancel(t.Context())
	gaveUp := make(chan error)
	go func() {
		_, err := visibility.IsPublic(ctx, "test-owner", "test-repo")
		gaveUp <- err
	}()
	<-arrived
	cancel()
	if err := <-gaveUp; !errors.Is(err, ErrVisibilityUnavailable) || !errors.Is(err, context.Canceled) {
		t.Fatalf("the caller that gave up: %v", err)
	}
	close(release)
	// The lookup is still the first one, joined or already answered.
	if public, err := visibility.IsPublic(t.Context(), "test-owner", "test-repo"); !public || err != nil {
		t.Errorf("%v %v", public, err)
	}
	if calls := calls.Load(); calls != 1 {
		t.Errorf("%d lookups", calls)
	}
}

func TestEachNameIsOnePathSegment(t *testing.T) {
	paths := make(chan string, 1)
	visibility := serve(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths <- r.URL.EscapedPath()
		w.WriteHeader(http.StatusNotFound)
	}), VisibilityTTL)
	if _, err := visibility.IsPublic(t.Context(), "Test-Owner/x", "../Test-Repo"); err != nil {
		t.Fatal(err)
	}
	if path, want := <-paths, "/repos/test-owner%2Fx/..%2Ftest-repo"; path != want {
		t.Errorf("asked for %s, not %s", path, want)
	}
}
