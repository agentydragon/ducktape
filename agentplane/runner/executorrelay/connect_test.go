package executorrelay

import (
	"context"
	"errors"
	"testing"
	"testing/synctest"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestEnqueueDirectExecutorFrameBackpressuresAndCancels(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		queue := make(chan directExecFrame, executorQueueDepth)
		for range executorQueueDepth {
			queue <- directExecFrame{}
		}
		result := make(chan error, 1)
		go func() { result <- enqueueDirectExecutorFrame(ctx, queue, directExecFrame{}) }()
		synctest.Wait()
		select {
		case err := <-result:
			t.Fatalf("enqueue with a full active stream returned %v", err)
		default:
		}
		<-queue
		if err := <-result; err != nil {
			t.Fatalf("enqueue after capacity returned = %v", err)
		}
		go func() { result <- enqueueDirectExecutorFrame(ctx, queue, directExecFrame{}) }()
		synctest.Wait()
		cancel()
		if err := <-result; !errors.Is(err, context.Canceled) {
			t.Fatalf("enqueue after stream cancellation = %v, want context.Canceled", err)
		}
	})
}

func TestManagerOpenBoundsStreamsAndReleasesSlots(t *testing.T) {
	m := New(Config{})
	connections := make([]*Connection, 0, maxExecutorStreamsPerRunner)
	for range maxExecutorStreamsPerRunner {
		connection, err := m.Open(t.Context(), 1, nil, nil)
		if err != nil {
			t.Fatal(err)
		}
		connections = append(connections, connection)
	}
	if _, err := m.Open(t.Context(), 1, nil, nil); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("ninth executor stream = %v, want ResourceExhausted", err)
	}
	connections[0].Close()
	connections[0].Close()
	replacement, err := m.Open(t.Context(), 1, nil, nil)
	if err != nil {
		t.Fatalf("open replacement executor stream: %v", err)
	}
	replacement.Close()
	for _, connection := range connections[1:] {
		connection.Close()
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.streams != 0 {
		t.Fatalf("active executor streams after close = %d, want 0", m.streams)
	}
}
