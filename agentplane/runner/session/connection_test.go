package session

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/harness"
)

func TestConnectionFailedBatchDoesNotDeliverReceipt(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	sent := make(chan struct{})
	c := &nativeConnection{deps: nativeConnectionDependencies{
		journal:    openTestJournal(t),
		send:       func(context.Context, json.RawMessage) error { close(sent); return nil },
		afterBatch: func() { t.Error("failed batch was published") },
	}}
	done := make(chan nativeResult, 1)
	go func() {
		receipt, err := c.request(ctx, json.RawMessage(`{"request":true}`), func(json.RawMessage) bool { return true })
		done <- nativeResult{receipt: receipt, err: err}
	}()
	select {
	case <-sent:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	failure := errors.New("frame translation failed")
	err := c.receiveBatch(ctx, [][]byte{[]byte(`{"reply":true}`)}, func(context.Context, []byte) error {
		c.match(harness.NativeReceipt{Frame: json.RawMessage(`{"reply":true}`), Sequence: 1})
		return failure
	})
	if !errors.Is(err, failure) {
		t.Fatalf("batch error = %v, want translation failure", err)
	}
	select {
	case reply := <-done:
		t.Fatalf("reply escaped failed batch: %+v", reply)
	default:
	}
	c.end(1)
	select {
	case reply := <-done:
		if reply.err == nil || reply.receipt.Sequence != 0 {
			t.Fatalf("failed batch receipt = %+v, want exit error without receipt", reply)
		}
	case <-ctx.Done():
		t.Fatal("connection exit did not release handshake waiter")
	}
}
