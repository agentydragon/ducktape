package liveinventory

import "sync"

// changeHub broadcasts coalesced notifications independently to every live stream.
type changeHub struct {
	mu          sync.Mutex
	nextID      uint64
	subscribers map[uint64]chan struct{}
}

func newChangeHub() changeHub {
	return changeHub{subscribers: make(map[uint64]chan struct{})}
}

func (h *changeHub) subscribe() (<-chan struct{}, func()) {
	h.mu.Lock()
	h.nextID++
	id := h.nextID
	channel := make(chan struct{}, 1)
	h.subscribers[id] = channel
	h.mu.Unlock()

	var once sync.Once
	return channel, func() {
		once.Do(func() {
			h.mu.Lock()
			delete(h.subscribers, id)
			close(channel)
			h.mu.Unlock()
		})
	}
}

func (h *changeHub) notify() {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, subscriber := range h.subscribers {
		select {
		case subscriber <- struct{}{}:
		default:
		}
	}
}
