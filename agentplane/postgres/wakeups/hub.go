// Package wakeups tells replica-local readers that the rows they follow may have changed.
//
// A NOTIFY carries a routing key and the committed rows stay the authority: a woken reader re-reads what it follows,
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

	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/lib/pq"
)

// Hub follows NOTIFY channels on one LISTEN connection and fans each out to its subscribers.
type Hub struct {
	listener   *pq.Listener
	connected  atomic.Bool
	generation atomic.Uint64
	mu         sync.Mutex
	channels   map[string]*channelSubscribers
}

type channelSubscribers struct {
	all      signals
	byKey    map[string]signals
	payloads map[*payloadSubscription]struct{}
}

type signals map[chan struct{}]struct{}

func (s signals) wake() {
	for signal := range s {
		select {
		case signal <- struct{}{}:
		default:
		}
	}
}

func newHub(channels ...string) *Hub {
	h := &Hub{channels: make(map[string]*channelSubscribers, len(channels))}
	for _, name := range channels {
		h.channels[name] = &channelSubscribers{
			all:      make(signals),
			byKey:    make(map[string]signals),
			payloads: make(map[*payloadSubscription]struct{}),
		}
	}
	return h
}

func (h *Hub) channel(name string) (*channelSubscribers, error) {
	if subscribers := h.channels[name]; subscribers != nil {
		return subscribers, nil
	}
	return nil, fmt.Errorf("not listening to channel %q", name)
}

// Listen opens the LISTEN connection to dsn and returns once it listens to every channel, or fails when ctx
// ends first. It reconnects by itself after that; Close ends it.
func Listen(ctx context.Context, dsn string, channels ...string) (*Hub, error) {
	hub := newHub(channels...)
	hub.listener = pq.NewListener(postgres.DriverURL(dsn), time.Second, 30*time.Second, func(event pq.ListenerEventType, err error) {
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

type payloadSubscription struct{ enqueue func(string) }

// SubscribePayload calls enqueue synchronously for every committed payload on channel.
// enqueue must return promptly (for example, workqueue.Add), and must not call Hub methods.
// Unlike a bounded channel, this hands off every key without dropping distinct payloads.
// Use Subscribe and Generation on the same channel to recover connection gaps.
func (h *Hub) SubscribePayload(channel string, enqueue func(string)) (func(), error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	subs, err := h.channel(channel)
	if err != nil {
		return nil, err
	}
	sub := &payloadSubscription{enqueue: enqueue}
	subs.payloads[sub] = struct{}{}
	return func() {
		h.mu.Lock()
		defer h.mu.Unlock()
		delete(subs.payloads, sub)
	}, nil
}

// SubscribeKey receives notifications whose payload is exactly key. On listener loss or reconnect
// it is also woken so the reader can discard stale state and establish a fresh snapshot.
func (h *Hub) SubscribeKey(channel, key string) (<-chan struct{}, func(), error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	subs, err := h.channel(channel)
	if err != nil {
		return nil, nil, err
	}
	if subs.byKey[key] == nil {
		subs.byKey[key] = make(signals)
	}
	signal := make(chan struct{}, 1)
	subs.byKey[key][signal] = struct{}{}
	return signal, func() {
		h.mu.Lock()
		defer h.mu.Unlock()
		delete(subs.byKey[key], signal)
		if len(subs.byKey[key]) == 0 {
			delete(subs.byKey, key)
		}
	}, nil
}

func (h *Hub) observeConnectionEvent(event pq.ListenerEventType) {
	switch event {
	case pq.ListenerEventConnected, pq.ListenerEventReconnected:
		h.generation.Add(1)
		h.connected.Store(true)
	case pq.ListenerEventDisconnected, pq.ListenerEventConnectionAttemptFailed:
		h.connected.Store(false)
		h.generation.Add(1)
		// Readers must show that their view may be stale during the reconnect gap,
		// rather than waiting for lib/pq's post-reconnect notification.
		h.wakeAll()
	}
}

// Connected reports the last connection state reported by lib/pq. It is suitable for live status
// frames, while Ping provides an active health check when a caller needs one.
func (h *Hub) Connected() bool { return h.connected.Load() }

// Generation changes on connection loss and establishment. A reader compares it before each
// read to distinguish ordinary keyed wakeups from a reconnect requiring a full snapshot.
func (h *Hub) Generation() uint64 { return h.generation.Load() }

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
	groups := make([]signals, 0, len(channels))
	for _, name := range channels {
		subs, err := h.channel(name)
		if err != nil {
			return nil, nil, err
		}
		groups = append(groups, subs.all)
	}
	signal := make(chan struct{}, 1)
	for _, group := range groups {
		group[signal] = struct{}{}
	}
	return signal, func() {
		h.mu.Lock()
		defer h.mu.Unlock()
		for _, group := range groups {
			delete(group, signal)
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
		h.notify(notification.Channel, notification.Extra)
	}
}

func (h *Hub) notify(channel, key string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if subs := h.channels[channel]; subs != nil {
		subs.all.wake()
		for sub := range subs.payloads {
			sub.enqueue(key)
		}
		subs.byKey[key].wake()
	}
}

func (h *Hub) wakeAll() {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, subs := range h.channels {
		subs.all.wake()
		for _, group := range subs.byKey {
			group.wake()
		}
	}
}
