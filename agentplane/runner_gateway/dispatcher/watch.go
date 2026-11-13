package dispatcher

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/internal/terminalnotice"
	"github.com/lib/pq"
)

type watchSubscription struct {
	changed chan struct{}
	// Nil for a subscriber that only needs invalidations. A bounded terminal buffer nobody drains
	// would overflow and fail the subscriber after its capacity of terminal events.
	terminal chan terminalnotice.Event
	failure  chan string
}

// WatchHub transports committed PostgreSQL invalidations and terminal
// snapshots to every live stream. The DB rows remain authoritative; a change
// notification only causes another read.
type WatchHub struct {
	listener    *pq.Listener
	mu          sync.Mutex
	subscribers map[*watchSubscription]struct{}
	pending     terminalnotice.Assembler
	connected   bool
	subscribed  bool
}

func NewWatchHub(dsn string) *WatchHub {
	hub := &WatchHub{
		subscribers: map[*watchSubscription]struct{}{},
	}
	hub.listener = pq.NewListener(dsn, time.Second, 30*time.Second, hub.connectionEvent)
	return hub
}

func (h *WatchHub) connectionEvent(event pq.ListenerEventType, err error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	switch event {
	case pq.ListenerEventConnected:
		h.connected = true
	case pq.ListenerEventReconnected:
		h.connected = true
		h.failAllLocked("terminal event listener generation changed; reconnect this WatchDispatches stream")
	case pq.ListenerEventDisconnected, pq.ListenerEventConnectionAttemptFailed:
		h.connected = false
		h.failAllLocked("terminal event listener disconnected; reconnect this WatchDispatches stream")
	}
	_ = err // Listener reconnects itself; stream failure is the observable contract.
}

// subscribe is a WatchDispatches stream's subscription: invalidations and terminal snapshots.
func (h *WatchHub) subscribe() (*watchSubscription, func(), error) {
	return h.add(&watchSubscription{make(chan struct{}, 1), make(chan terminalnotice.Event, 32), make(chan string, 1)})
}

// subscribeChanges receives invalidations and listener failures but no terminal snapshots.
func (h *WatchHub) subscribeChanges() (*watchSubscription, func(), error) {
	return h.add(&watchSubscription{changed: make(chan struct{}, 1), failure: make(chan string, 1)})
}

func (h *WatchHub) add(sub *watchSubscription) (*watchSubscription, func(), error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if !h.connected {
		return nil, nil, errors.New("PostgreSQL watch listener is disconnected")
	}
	h.subscribers[sub] = struct{}{}
	return sub, func() { h.mu.Lock(); delete(h.subscribers, sub); h.mu.Unlock() }, nil
}

// Connected reports whether the PostgreSQL listener has established its
// current generation. The composition root waits for this before serving.
func (h *WatchHub) Connected() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.connected && h.subscribed
}

func (h *WatchHub) failAllLocked(message string) {
	h.pending.Reset()
	for sub := range h.subscribers {
		select {
		case sub.failure <- message:
		default:
		}
	}
}

func (h *WatchHub) notifyChanged() {
	h.mu.Lock()
	defer h.mu.Unlock()
	for sub := range h.subscribers {
		select {
		case sub.changed <- struct{}{}:
		default:
		}
	}
}

func (h *WatchHub) notifyTerminal(event terminalnotice.Event) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for sub := range h.subscribers {
		if sub.terminal == nil {
			continue
		}
		select {
		case sub.terminal <- event:
		default:
			select {
			case sub.failure <- "terminal event subscription overflowed; reconnect this WatchDispatches stream":
			default:
			}
		}
	}
}

// Run owns the listener for the service lifetime. A reconnect ends active
// streams rather than silently losing a terminal event during the gap.
func (h *WatchHub) Run(ctx context.Context) error {
	defer h.listener.Close()
	if err := h.listener.Listen(NotifyChannel); err != nil {
		return err
	}
	if err := h.listener.Listen(TerminalChannel); err != nil {
		return err
	}
	h.mu.Lock()
	h.subscribed = true
	h.mu.Unlock()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case item, ok := <-h.listener.Notify:
			if !ok {
				return errors.New("PostgreSQL listener closed")
			}
			if item == nil {
				h.notifyChanged()
				continue
			}
			switch item.Channel {
			case NotifyChannel:
				h.notifyChanged()
			case TerminalChannel:
				if err := h.terminalPart(item.Extra); err != nil {
					h.mu.Lock()
					h.failAllLocked("terminal event notification was malformed; reconnect this WatchDispatches stream")
					h.mu.Unlock()
				}
			}
		case now := <-ticker.C:
			if h.pending.HasExpired(now, 10*time.Second) {
				h.mu.Lock()
				h.failAllLocked("terminal event parts were incomplete; reconnect this WatchDispatches stream")
				h.mu.Unlock()
			}
		}
	}
}

func (h *WatchHub) terminalPart(payload string) error {
	event, err := h.pending.Add(payload)
	if err != nil {
		return err
	}
	if event != nil {
		h.notifyTerminal(*event)
	}
	return nil
}
