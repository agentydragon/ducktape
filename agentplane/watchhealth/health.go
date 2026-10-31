// Package watchhealth says whether a service's informer caches may be acted on, the way client-go
// controllers judge it: the cache is ready once every informer has synced, and a watched kind is
// unhealthy while its list or watch requests are failing. client-go owns the watch lifetime,
// reconnects, resumes and relists; a dead connection is caught by its default HTTP/2 health checks
// and surfaces here as a failed request.
package watchhealth

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"sync"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
)

// Kind is a watched resource kind, by plural or another stable name.
type Kind string

// Failure is why a kind is unhealthy, since the first failure after its last success.
type Failure struct {
	Error string
	Since time.Time
}

// Health tracks one service's informer caches.
type Health struct {
	now func() time.Time

	mu       sync.RWMutex
	failing  map[Kind]Failure
	synced   bool
	draining bool
}

// New returns a Health that is unavailable until MarkSynced.
func New() *Health {
	return &Health{now: time.Now, failing: map[Kind]Failure{}}
}

// Status is the facts Available rests on, read at one moment.
type Status struct {
	Available bool
	// Synced is whether every informer has listed once.
	Synced   bool
	Draining bool
	// Failing holds each kind whose last list or watch request failed.
	Failing map[Kind]Failure
}

// Available reports whether the caches may be acted on: synced, not draining, and no kind failing.
func (h *Health) Available() bool {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.synced && !h.draining && len(h.failing) == 0
}

// Status reads the verdict and the failures it rests on under one lock.
func (h *Health) Status() Status {
	h.mu.RLock()
	defer h.mu.RUnlock()
	failing := make(map[Kind]Failure, len(h.failing))
	for kind, failure := range h.failing {
		failing[kind] = failure
	}
	return Status{
		Available: h.synced && !h.draining && len(h.failing) == 0,
		Synced:    h.synced, Draining: h.draining, Failing: failing,
	}
}

// MarkSynced records that every informer has synced. It never falls back: a failing kind is what
// makes a synced cache unavailable after.
func (h *Health) MarkSynced() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.synced = true
}

// MarkDraining makes the caches unavailable for good: the service admits nothing new.
func (h *Health) MarkDraining() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.draining = true
}

// RecordSuccess clears kind's failure: one of its lists or watches succeeded.
func (h *Health) RecordSuccess(kind Kind) {
	h.mu.Lock()
	defer h.mu.Unlock()
	delete(h.failing, kind)
}

// RecordFailure marks kind failing until its next success, keeping when its failures began.
func (h *Health) RecordFailure(kind Kind, err error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	failure, ok := h.failing[kind]
	if !ok {
		failure.Since = h.now()
	}
	failure.Error = err.Error()
	h.failing[kind] = failure
}

// Config is a copy of base whose list and watch requests record their kind's health: a successful
// response clears the kind's failure, and a failed request or error status records one. kindOf names
// the kind a request reads.
func (h *Health) Config(base *rest.Config, kindOf func(*url.URL) Kind) *rest.Config {
	recorded := rest.CopyConfig(base)
	recorded.Wrap(func(next http.RoundTripper) http.RoundTripper {
		return &recordingTransport{next: next, health: h, kindOf: kindOf}
	})
	return recorded
}

// KindByPath is the kind a request's path names, by plural.
func KindByPath(requested *url.URL) Kind { return Kind(path.Base(requested.Path)) }

// RecordingAs names every request kind, for a resource watched in several namespaces whose path
// alone does not tell them apart.
func RecordingAs(kind Kind) func(*url.URL) Kind { return func(*url.URL) Kind { return kind } }

type recordingTransport struct {
	next   http.RoundTripper
	health *Health
	kindOf func(*url.URL) Kind
}

// WrappedRoundTripper lets client-go see through to the transport it configured.
func (t *recordingTransport) WrappedRoundTripper() http.RoundTripper { return t.next }

// RoundTrip records only GETs, the informers' lists and watches; writes made through the same
// config are not reads of the cache.
func (t *recordingTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	response, err := t.next.RoundTrip(request)
	if request.Method != http.MethodGet {
		return response, err
	}
	kind := t.kindOf(request.URL)
	switch {
	case err != nil:
		if !errors.Is(err, context.Canceled) {
			t.health.RecordFailure(kind, err)
		}
	case response.StatusCode >= 200 && response.StatusCode < 300:
		t.health.RecordSuccess(kind)
	case response.StatusCode == http.StatusGone:
		// An expired resourceVersion: client-go relists, as it does after any 410.
	default:
		t.health.RecordFailure(kind, fmt.Errorf("%s %s: %s", request.Method, request.URL.Path, response.Status))
	}
	return response, err
}

// WatchErrorHandler records the errors that end an informer's ListAndWatch, such as a response that
// does not decode, which the transport sees as successful. It keeps client-go's logging. A watch
// closed normally or on an expired resourceVersion is client-go's ordinary resume and relist.
func (h *Health) WatchErrorHandler(kind Kind) cache.WatchErrorHandlerWithContext {
	return func(ctx context.Context, r *cache.Reflector, err error) {
		cache.DefaultWatchErrorHandler(ctx, r, err)
		if errors.Is(err, io.EOF) || apierrors.IsResourceExpired(err) || apierrors.IsGone(err) || errors.Is(err, context.Canceled) {
			return
		}
		h.RecordFailure(kind, err)
	}
}
