// Package wakeups tells replica-local readers that the rows they follow may have changed.
//
// A NOTIFY carries nothing and the committed rows stay the authority: a woken reader re-reads what it follows,
// so commits on any replica, and commits made while the LISTEN connection was down, are read from the tables.
// Re-establishing the connection wakes every channel for the same reason.
package wakeups

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/lib/pq"
)

// Hub follows NOTIFY channels on one LISTEN connection and fans each out to its subscribers.
type Hub struct {
	listener  *pq.Listener
	connected atomic.Bool
	mu        sync.Mutex
	// subscribers holds each listened channel's subscribers' signals.
	subscribers map[string]map[chan struct{}]struct{}
}

// Listen opens the LISTEN connection to dsn and returns once it listens to every channel, or fails when ctx
// ends first. It reconnects by itself after that; Close ends it.
func Listen(ctx context.Context, dsn string, channels ...string) (*Hub, error) {
	hub := &Hub{subscribers: make(map[string]map[chan struct{}]struct{}, len(channels))}
	for _, channel := range channels {
		hub.subscribers[channel] = map[chan struct{}]struct{}{}
	}
	hub.listener = pq.NewListener(dsn, time.Second, 30*time.Second, func(event pq.ListenerEventType, err error) {
		hub.observeConnectionEvent(event)
		if err != nil {
			// The error text can carry the connection string; the type and SQLSTATE name the failure.
			slog.Warn("PostgreSQL listener event failed", "event", event, "error_type", fmt.Sprintf("%T", err), "sqlstate", sqlState(err))
		}
	})
	listening := make(chan error, 1)
	go func() {
		for _, channel := range channels {
			if err := hub.listener.Listen(channel); err != nil {
				listening <- err
				return
			}
		}
		listening <- nil
	}()
	select {
	case err := <-listening:
		if err != nil {
			hub.listener.Close()
			return nil, err
		}
	case <-ctx.Done():
		// Closing the listener ends the Listen call still waiting for a connection.
		hub.listener.Close()
		return nil, ctx.Err()
	}
	go hub.fanOut()
	return hub, nil
}

func (h *Hub) observeConnectionEvent(event pq.ListenerEventType) {
	switch event {
	case pq.ListenerEventConnected, pq.ListenerEventReconnected:
		h.connected.Store(true)
	case pq.ListenerEventDisconnected, pq.ListenerEventConnectionAttemptFailed:
		h.connected.Store(false)
	}
}

// Connected reports the last connection state reported by lib/pq. It is suitable for live status
// frames, while Ping provides an active health check when a caller needs one.
func (h *Hub) Connected() bool { return h.connected.Load() }

func sqlState(err error) string {
	if failure, ok := err.(*pq.Error); ok {
		return string(failure.Code)
	}
	return ""
}

// Subscribe returns a signal that holds a pending wakeup whenever one of channels was notified, and the function
// that ends the subscription. Wakeups coalesce: a burst of notifications leaves one signal to receive, so a
// reader that clears its signal and then reads the current state never works through a backlog of stale ones.
func (h *Hub) Subscribe(channels ...string) (<-chan struct{}, func(), error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, channel := range channels {
		if _, listening := h.subscribers[channel]; !listening {
			return nil, nil, fmt.Errorf("not listening to channel %q", channel)
		}
	}
	signal := make(chan struct{}, 1)
	for _, channel := range channels {
		h.subscribers[channel][signal] = struct{}{}
	}
	return signal, func() {
		h.mu.Lock()
		defer h.mu.Unlock()
		for _, channel := range channels {
			delete(h.subscribers[channel], signal)
		}
	}, nil
}

// Close ends the LISTEN connection.
func (h *Hub) Close() error { return h.listener.Close() }

// Ping is a round trip on the LISTEN connection. It fails while the connection is down or being re-established,
// during which notifications are lost: a service that cannot show stale answers is not ready until it succeeds.
func (h *Hub) Ping() error { return h.listener.Ping() }

func (h *Hub) fanOut() {
	for notification := range h.listener.Notify {
		// The listener sends nil after it reconnected: notifications in between were lost.
		if notification == nil {
			h.wakeAll()
			continue
		}
		h.wake(notification.Channel)
	}
}

func (h *Hub) wake(channels ...string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, channel := range channels {
		for signal := range h.subscribers[channel] {
			select {
			case signal <- struct{}{}:
			default:
			}
		}
	}
}

func (h *Hub) wakeAll() {
	h.mu.Lock()
	channels := make([]string, 0, len(h.subscribers))
	for channel := range h.subscribers {
		channels = append(channels, channel)
	}
	h.mu.Unlock()
	h.wake(channels...)
}
