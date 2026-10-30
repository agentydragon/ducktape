package admin

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/google/uuid"
)

var started = time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)

type fakeDecisions struct {
	records []decision.Record
	err     error
	asked   []*decision.Subject
}

func (f *fakeDecisions) Recent(_ context.Context, subject *decision.Subject) ([]decision.Record, error) {
	f.asked = append(f.asked, subject)
	return f.records, f.err
}

type fakeChanges func(ctx context.Context, changed func() error) error

func (f fakeChanges) Follow(ctx context.Context, changed func() error) error { return f(ctx, changed) }

type fakeHistory history.Health

func (f fakeHistory) Health() history.Health { return history.Health(f) }

type fakeIndex watchhealth.Status

func (f fakeIndex) Status() watchhealth.Status { return watchhealth.Status(f) }

func healthyIndex() fakeIndex {
	return fakeIndex{Available: true, Synced: true}
}

func failingIndex() fakeIndex {
	return fakeIndex{Synced: true, Failing: map[watchhealth.Kind]watchhealth.Failure{
		policy.Secrets: {Error: "test-failure", Since: started},
	}}
}

func drainingIndex() fakeIndex {
	return fakeIndex{Synced: true, Draining: true}
}

func record(subject *decision.Subject) decision.Record {
	return decision.Record{
		EventID: uuid.New(), At: started, ProducerID: uuid.New(), Subject: subject, ConnectionID: "test-connection",
		Phase: decision.HTTPRequest, Method: "GET", Host: "example.test", Port: 443, Outcome: decision.Allow,
	}
}
