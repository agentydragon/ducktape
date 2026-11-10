package admin

import (
	"context"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/google/uuid"
)

type windowTestDecisions struct {
	mu          sync.Mutex
	initial     []decision.Record
	records     map[uuid.UUID]decision.Record
	recentCalls int
	eventCalls  int
	readCalls   int
}

func (d *windowTestDecisions) Recent(_ context.Context, subject decision.Subject) ([]decision.Record, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.recentCalls++
	result := make([]decision.Record, 0, len(d.initial))
	for _, record := range d.initial {
		if record.Subject == subject {
			result = append(result, record)
		}
	}
	return result, nil
}

func (d *windowTestDecisions) ByEventIDs(_ context.Context, ids []uuid.UUID) ([]decision.Record, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.eventCalls++
	var result []decision.Record
	for _, id := range ids {
		if record, ok := d.records[id]; ok {
			result = append(result, record)
		}
	}
	return result, nil
}

func (d *windowTestDecisions) Readable(context.Context) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.readCalls++
	return nil
}

func (*windowTestDecisions) Window() history.Window {
	return history.Window{Retention: 24 * time.Hour, Capacity: 2}
}

type windowTestChanges struct {
	callbacks chan func(history.Change) error
	mu        sync.Mutex
	follows   int
}

func (c *windowTestChanges) Follow(ctx context.Context, _ decision.Subject, changed func(history.Change) error) error {
	if err := changed(history.Initial{}); err != nil {
		return err
	}
	c.mu.Lock()
	c.follows++
	c.mu.Unlock()
	c.callbacks <- changed
	<-ctx.Done()
	return ctx.Err()
}

func TestSubjectWindowSharesSnapshotAndReadsCommittedIDsIncrementally(t *testing.T) {
	subject := decision.ServiceAccount{Namespace: "test-ns", Name: "test-app"}
	now := time.Now()
	oldest := windowTestRecord(subject, now.Add(-3*time.Minute))
	newest := windowTestRecord(subject, now.Add(-time.Minute))
	late := windowTestRecord(subject, now.Add(-2*time.Minute))
	decisions := &windowTestDecisions{
		initial: []decision.Record{oldest, newest},
		records: map[uuid.UUID]decision.Record{late.EventID: late},
	}
	changes := &windowTestChanges{callbacks: make(chan func(history.Change) error, 1)}
	windows := newDecisionWindows(decisions, changes)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	first, firstSnapshot, err := windows.subscribe(ctx, subject)
	if err != nil {
		t.Fatal(err)
	}
	second, secondSnapshot, err := windows.subscribe(ctx, subject)
	if err != nil {
		t.Fatal(err)
	}
	if len(firstSnapshot) != 2 || len(secondSnapshot) != 2 || firstSnapshot[0].GetEventId() != oldest.EventID.String() {
		t.Fatalf("snapshots = %v and %v, want the same two-record window", firstSnapshot, secondSnapshot)
	}
	changed := <-changes.callbacks
	if decisions.recentCalls != 1 || changes.follows != 1 {
		t.Fatalf("same-subject watchers used %d snapshots and %d feeds, want one each", decisions.recentCalls, changes.follows)
	}

	if err := changed(history.Events{EventIDs: []uuid.UUID{late.EventID}}); err != nil {
		t.Fatal(err)
	}
	for i, subscriber := range []*windowSubscriber{first, second} {
		select {
		case update := <-subscriber.updates:
			if update.delta == nil || len(update.delta.Added) != 1 || update.delta.Added[0].GetEventId() != late.EventID.String() ||
				!slices.Equal(update.delta.RemovedEventIds, []string{oldest.EventID.String()}) {
				t.Fatalf("subscriber %d update = %+v, want late add and capacity eviction", i, update)
			}
		case <-time.After(time.Second):
			t.Fatalf("subscriber %d did not receive shared delta", i)
		}
	}
	if err := changed(history.Heartbeat{}); err != nil {
		t.Fatal(err)
	}
	if err := changed(history.Heartbeat{}); err != nil {
		t.Fatal(err)
	}
	if decisions.eventCalls != 1 || decisions.recentCalls != 1 || decisions.readCalls != 1 {
		t.Fatalf("reads: snapshots=%d event-ID=%d health=%d; want 1, 1, 1", decisions.recentCalls, decisions.eventCalls, decisions.readCalls)
	}
	for i, subscriber := range []*windowSubscriber{first, second} {
		for range 2 {
			select {
			case update := <-subscriber.updates:
				if !update.heartbeat {
					t.Fatalf("subscriber %d got non-heartbeat %+v", i, update)
				}
			case <-time.After(time.Second):
				t.Fatalf("subscriber %d missed shared heartbeat", i)
			}
		}
	}

	windows.unsubscribe(subject, first)
	windows.unsubscribe(subject, second)
}

func TestBoundedWindowExpiresOldEntriesAndRetainsNewestCapacity(t *testing.T) {
	subject := decision.ServiceAccount{Namespace: "test-ns", Name: "test-app"}
	now := time.Now()
	expired := windowTestRecord(subject, now.Add(-2*time.Hour))
	older := windowTestRecord(subject, now.Add(-3*time.Minute))
	newer := windowTestRecord(subject, now.Add(-time.Minute))
	late := windowTestRecord(subject, now.Add(-2*time.Minute))
	got := boundedWindow([]decision.Record{expired, newer}, []decision.Record{older, late}, history.Window{
		Retention: time.Hour, Capacity: 2,
	})
	if len(got) != 2 || got[0].EventID != late.EventID || got[1].EventID != newer.EventID {
		t.Fatalf("window = %+v, want newest two in decision-time order and no expired record", got)
	}
}

func TestFailedWaiterCleanupPreservesReplacementWindow(t *testing.T) {
	subject := decision.ServiceAccount{Namespace: "test-ns", Name: "test-app"}
	oldCanceled := false
	old := &subjectWindow{
		subject: subject,
		cancel: func() {
			oldCanceled = true
		},
	}
	replacement := &subjectWindow{subject: subject, cancel: func() {}}
	windows := &decisionWindows{active: map[decision.Subject]*subjectWindow{subject: replacement}}

	windows.mu.Lock()
	windows.stopIfUnusedLocked(old)
	windows.mu.Unlock()

	if windows.active[subject] != replacement {
		t.Fatal("stale failed feed cleanup removed the replacement subject window")
	}
	if oldCanceled {
		t.Fatal("stale failed feed cleanup canceled the old generation")
	}
}

func windowTestRecord(subject decision.Subject, at time.Time) decision.Record {
	return decision.Record{EventID: uuid.New(), At: at, Subject: subject}
}
