package main

import (
	"context"
	"log/slog"
	"sync"
)

// frameTopics shares one producer per key among every live stream that subscribes with that key, so a change is
// computed once however many tabs watch it. A key must hold every input the produced frame depends on: frames are
// shared between callers, so nothing produced may depend on who is asking.
//
// The producer runs from the first subscription until the last one ends, publishing each new frame; a subscriber
// sees the latest frame (a slow one skips frames it had not yet read) and its channel closes when the producer
// returns, after which the next subscription starts a new producer.
type frameTopics[K comparable, V any] struct {
	produce func(ctx context.Context, key K, publish func(V)) error
	logger  func() *slog.Logger

	mu     sync.Mutex
	topics map[K]*frameTopic[V]
}

type frameTopic[V any] struct {
	stop        context.CancelFunc
	latest      V
	published   bool
	subscribers map[chan V]struct{}
}

func newFrameTopics[K comparable, V any](logger func() *slog.Logger, produce func(context.Context, K, func(V)) error) *frameTopics[K, V] {
	return &frameTopics[K, V]{produce: produce, logger: logger, topics: make(map[K]*frameTopic[V])}
}

// subscribe returns the frames for key, starting with the latest one if the topic already has it, and the function
// that ends this subscription.
func (h *frameTopics[K, V]) subscribe(key K) (<-chan V, func()) {
	h.mu.Lock()
	defer h.mu.Unlock()
	topic := h.topics[key]
	if topic == nil {
		// Producers outlive the request that started them: they serve every later subscriber with the same key.
		ctx, stop := context.WithCancel(context.Background())
		topic = &frameTopic[V]{stop: stop, subscribers: make(map[chan V]struct{})}
		h.topics[key] = topic
		go h.run(ctx, key, topic)
	}
	frames := make(chan V, 1)
	if topic.published {
		frames <- topic.latest
	}
	topic.subscribers[frames] = struct{}{}
	return frames, func() { h.unsubscribe(key, topic, frames) }
}

func (h *frameTopics[K, V]) run(ctx context.Context, key K, topic *frameTopic[V]) {
	err := h.produce(ctx, key, func(frame V) { h.publish(topic, frame) })
	if err != nil && ctx.Err() == nil {
		h.logger().WarnContext(ctx, "live stream snapshot failed", "error", err)
	}
	topic.stop()
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.topics[key] == topic {
		delete(h.topics, key)
	}
	for frames := range topic.subscribers {
		close(frames)
	}
	topic.subscribers = nil
}

func (h *frameTopics[K, V]) publish(topic *frameTopic[V], frame V) {
	h.mu.Lock()
	defer h.mu.Unlock()
	topic.latest, topic.published = frame, true
	for frames := range topic.subscribers {
		// Only publish sends, under h.mu, so after dropping an unread frame the buffer has room.
		select {
		case <-frames:
		default:
		}
		frames <- frame
	}
}

func (h *frameTopics[K, V]) unsubscribe(key K, topic *frameTopic[V], frames chan V) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := topic.subscribers[frames]; !ok {
		return
	}
	delete(topic.subscribers, frames)
	if len(topic.subscribers) == 0 && h.topics[key] == topic {
		delete(h.topics, key)
		topic.stop()
	}
}
