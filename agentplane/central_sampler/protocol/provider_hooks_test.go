package protocol

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func TestProviderResponseHeaderTimeoutCoversHeaderWait(t *testing.T) {
	started := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-r.Context().Done()
	}))
	defer server.Close()

	client := WithProviderResponseHeaderTimeout(server.Client(), 25*time.Millisecond)
	response, err := client.Get(server.URL)
	if response != nil {
		_ = response.Body.Close()
		t.Fatal("received response headers from a stalled provider")
	}
	var timeout *ProviderPhaseTimeoutError
	if !errors.As(err, &timeout) || timeout.Phase != ProviderTimeoutResponseHeader {
		t.Fatalf("response-header error=%v, want typed response-header timeout", err)
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("server did not receive the request")
	}
}

func TestProviderResponseHeaderTimeoutBoundsBlockedRequestBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
	}))
	defer server.Close()

	body := &blockedRequestBody{started: make(chan struct{}), closed: make(chan struct{})}
	request, err := http.NewRequest(http.MethodPost, server.URL, body)
	if err != nil {
		t.Fatal(err)
	}
	request.ContentLength = -1
	response, err := WithProviderResponseHeaderTimeout(server.Client(), 25*time.Millisecond).Do(request)
	if response != nil {
		_ = response.Body.Close()
		t.Fatal("received response headers while the request body was blocked")
	}
	var timeout *ProviderPhaseTimeoutError
	if !errors.As(err, &timeout) || timeout.Phase != ProviderTimeoutResponseHeader {
		t.Fatalf("blocked request-body error=%v, want typed response-header timeout", err)
	}
	select {
	case <-body.started:
	case <-time.After(time.Second):
		t.Fatal("transport never began reading the request body")
	}
	select {
	case <-body.closed:
	case <-time.After(time.Second):
		t.Fatal("timeout did not close the blocked request body")
	}
}

type blockedRequestBody struct {
	started     chan struct{}
	closed      chan struct{}
	startedOnce sync.Once
	closedOnce  sync.Once
}

func (body *blockedRequestBody) Read([]byte) (int, error) {
	body.startedOnce.Do(func() { close(body.started) })
	<-body.closed
	return 0, io.ErrClosedPipe
}

func (body *blockedRequestBody) Close() error {
	body.closedOnce.Do(func() { close(body.closed) })
	return nil
}

func TestProviderResponseHeaderTimeoutLeavesStreamingBodyLive(t *testing.T) {
	const body = "event: response.completed\ndata: {}\n\n"
	headersFlushed := make(chan struct{})
	releaseBody := make(chan struct{})
	requestWasCanceled := make(chan bool, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		close(headersFlushed)
		<-releaseBody
		requestWasCanceled <- r.Context().Err() != nil
		_, _ = fmt.Fprint(w, body)
	}))
	defer server.Close()

	client := WithProviderResponseHeaderTimeout(server.Client(), 25*time.Millisecond)
	response, err := client.Get(server.URL)
	if err != nil {
		t.Fatalf("response headers returned an error: %v", err)
	}
	defer response.Body.Close()
	select {
	case <-headersFlushed:
	case <-time.After(time.Second):
		t.Fatal("server did not flush response headers")
	}
	// Keep the response body behind a gate longer than the header budget. The server
	// context must remain live until the caller closes the streaming body.
	select {
	case <-time.After(75 * time.Millisecond):
	case <-requestWasCanceled:
		t.Fatal("request context was canceled before the body gate opened")
	}
	close(releaseBody)
	select {
	case canceled := <-requestWasCanceled:
		if canceled {
			t.Fatal("request context was canceled when RoundTrip returned response headers")
		}
	case <-time.After(time.Second):
		t.Fatal("server did not continue response body after headers")
	}
	got, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatalf("stream body read after headers: %v", err)
	}
	if string(got) != body {
		t.Fatalf("stream body=%q, want %q", got, body)
	}
}

func TestProviderTransportErrorsBeforeRequestWriteAreNotHeaderTimeouts(t *testing.T) {
	want := errors.New("fixture connection failure")
	client := &http.Client{Transport: roundTripperFunc(func(*http.Request) (*http.Response, error) { return nil, want })}
	_, err := WithProviderResponseHeaderTimeout(client, time.Second).Get("https://fixture.invalid")
	var timeout *ProviderPhaseTimeoutError
	if !errors.Is(err, want) || errors.As(err, &timeout) {
		t.Fatalf("pre-write transport error=%v, want original connection failure", err)
	}
}

func TestProviderResponseHeaderTimeoutPreservesCallerCancellationCause(t *testing.T) {
	want := errors.New("caller interrupted provider request")
	started := make(chan struct{})
	client := &http.Client{Transport: roundTripperFunc(func(request *http.Request) (*http.Response, error) {
		close(started)
		<-request.Context().Done()
		return nil, context.Cause(request.Context())
	})}
	ctx, cancel := context.WithCancelCause(context.Background())
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://fixture.invalid", nil)
	if err != nil {
		t.Fatal(err)
	}
	responseErr := make(chan error, 1)
	go func() {
		_, err := WithProviderResponseHeaderTimeout(client, time.Second).Do(request)
		responseErr <- err
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("transport did not start the request")
	}
	cancel(want)
	select {
	case err := <-responseErr:
		var timeout *ProviderPhaseTimeoutError
		if !errors.Is(err, want) || errors.As(err, &timeout) {
			t.Fatalf("caller cancellation error=%v, want original cause without phase timeout", err)
		}
	case <-time.After(time.Second):
		t.Fatal("transport did not return after caller cancellation")
	}
}

func TestProviderIdleTimeoutStartsAtDurableHeadAndResetsOnProgress(t *testing.T) {
	t.Run("timeout starts when head is recorded", func(t *testing.T) {
		ctx, hooks, stop := WithProviderIdleTimeout(context.Background(), 30*time.Millisecond, nil)
		defer stop()
		select {
		case <-ctx.Done():
			t.Fatal("idle timeout started before the response head was recorded")
		case <-time.After(45 * time.Millisecond):
		}
		hooks.ResponseHeadRecorded()
		select {
		case <-ctx.Done():
			var timeout *ProviderPhaseTimeoutError
			if !errors.As(context.Cause(ctx), &timeout) || timeout.Phase != ProviderTimeoutSemanticIdle {
				t.Fatalf("idle cancellation cause=%v, want typed semantic-idle timeout", context.Cause(ctx))
			}
		case <-time.After(time.Second):
			t.Fatal("semantic-idle budget did not cancel the provider context")
		}
	})

	t.Run("durable progress renews the semantic idle budget", func(t *testing.T) {
		ctx, hooks, stop := WithProviderIdleTimeout(context.Background(), 300*time.Millisecond, nil)
		defer stop()
		hooks.ResponseHeadRecorded()
		for range 3 {
			select {
			case <-ctx.Done():
				t.Fatalf("provider idle context ended before semantic progress: %v", context.Cause(ctx))
			case <-time.After(120 * time.Millisecond):
			}
			if err := hooks.Progress(ctx); err != nil {
				t.Fatal(err)
			}
		}
		if cause := context.Cause(ctx); cause != nil {
			t.Fatalf("provider idle context ended despite progress: %v", cause)
		}
	})
}

func TestProviderProgressCallbackFailureHasDistinctType(t *testing.T) {
	want := errors.New("fixture lease journal failure")
	ctx, hooks, stop := WithProviderIdleTimeout(context.Background(), time.Second, func(context.Context) error { return want })
	defer stop()
	hooks.ResponseHeadRecorded()
	err := hooks.Progress(ctx)
	var progressErr *ProviderProgressError
	if !errors.As(err, &progressErr) || !errors.Is(err, want) {
		t.Fatalf("progress callback error=%v, want distinct wrapped journal failure", err)
	}
}

func TestProviderTransportClassificationAndRetryAfter(t *testing.T) {
	want := errors.New("fixture connection reset")
	classified := ProviderTransportError(context.Background(), want)
	var transient *ProviderTransientError
	if !errors.As(classified, &transient) || !errors.Is(classified, want) {
		t.Fatalf("transport error=%v, want typed transient wrapping the cause", classified)
	}
	if got := ParseProviderRetryAfter("17"); got != 17*time.Second {
		t.Fatalf("delta Retry-After=%s, want 17s", got)
	}
	for _, value := range []string{"", "invalid", "0", "-2"} {
		if got := ParseProviderRetryAfter(value); got != 0 {
			t.Fatalf("Retry-After %q parsed as %s, want no hint", value, got)
		}
	}
	if got := ParseProviderRetryAfter("999999999999999999999999"); got != time.Duration(1<<63-1) {
		t.Fatalf("overflow Retry-After=%s, want saturated duration", got)
	}
	date := time.Now().Add(3 * time.Second).UTC().Format(http.TimeFormat)
	if got := ParseProviderRetryAfter(date); got <= 0 || got > 3*time.Second {
		t.Fatalf("HTTP-date Retry-After=%s, want positive delay up to 3s", got)
	}
}

func TestProviderEvidenceAndCallerCancellationAreNotTransient(t *testing.T) {
	persistence := &ProviderEvidencePersistenceError{Cause: errors.New("database unavailable")}
	if got := ProviderTransportError(context.Background(), persistence); got != persistence {
		t.Fatalf("evidence persistence failure was classified as transport: %v", got)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	cause := context.Canceled
	if got := ProviderTransportError(ctx, cause); !errors.Is(got, context.Canceled) {
		t.Fatalf("caller cancellation changed classification: %v", got)
	} else {
		var transient *ProviderTransientError
		if errors.As(got, &transient) {
			t.Fatalf("caller cancellation became provider-transient: %v", got)
		}
	}
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (fn roundTripperFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return fn(request)
}
