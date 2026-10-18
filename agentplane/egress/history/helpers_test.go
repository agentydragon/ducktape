package history

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/google/uuid"
	"github.com/sethvargo/go-retry"
)

const retention = 7 * 24 * time.Hour

// fastRetries keeps the write attempts but not their waits.
func fastRetries() retry.Backoff {
	return retry.WithMaxRetries(writeAttempts-1, retry.NewConstant(time.Millisecond))
}

func testLogConfig() LogConfig {
	return LogConfig{QueueSize: 2000, BatchSize: 100, FlushTimeout: 5 * time.Second}
}

func testRecord(at time.Time) decision.Record {
	return decision.Record{
		EventID: uuid.New(), At: at, ProducerID: uuid.New(), ConnectionID: "test-connection",
		Phase: decision.HTTPRequest, Method: "GET", Host: "example.test", Port: 443, Outcome: decision.Allow,
		Subject: &decision.Subject{Namespace: "test-namespace", Name: "test-workload"},
	}
}

func flush(t *testing.T, l *Log) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := l.Flush(ctx); err != nil {
		t.Fatal(err)
	}
}
