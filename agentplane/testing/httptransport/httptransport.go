// Package httptransport provides reusable HTTP transport helpers for Agentplane tests.
package httptransport

import (
	"io"
	"net/http"
	"sync"
)

// RoundTripperFunc adapts a function to http.RoundTripper.
type RoundTripperFunc func(*http.Request) (*http.Response, error)

func (f RoundTripperFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

// WatchStreams tracks open response bodies by key so tests can close streams for selected resources.
type WatchStreams[K comparable] struct {
	mu      sync.Mutex
	streams map[K][]io.Closer
}

// Add records an open watch response body.
func (s *WatchStreams[K]) Add(key K, stream io.Closer) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.streams == nil {
		s.streams = map[K][]io.Closer{}
	}
	s.streams[key] = append(s.streams[key], stream)
}

// Close closes and forgets every open stream under key.
func (s *WatchStreams[K]) Close(key K) {
	s.CloseMatching(func(candidate K) bool { return candidate == key })
}

// CloseMatching closes and forgets every open stream whose key matches.
func (s *WatchStreams[K]) CloseMatching(matches func(K) bool) {
	s.mu.Lock()
	var open []io.Closer
	for key, streams := range s.streams {
		if matches(key) {
			open = append(open, streams...)
			delete(s.streams, key)
		}
	}
	s.mu.Unlock()
	for _, stream := range open {
		_ = stream.Close()
	}
}
