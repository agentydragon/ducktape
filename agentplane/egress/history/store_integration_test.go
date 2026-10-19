package history

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/google/uuid"
)

func testDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := Open(integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func testStore(t *testing.T, capacity int) *Store {
	t.Helper()
	store, err := NewStore(testDB(t), retention, capacity)
	if err != nil {
		t.Fatal(err)
	}
	return store
}

// uniqueSubject keeps tests that share a database out of each other's reads.
func uniqueSubject() *decision.Subject {
	return &decision.Subject{Namespace: "test-" + uuid.NewString(), Name: "test-workload"}
}

// stored is a record as the database round-trips it: microsecond precision, no monotonic clock.
func stored(subject *decision.Subject, at time.Time) decision.Record {
	record := testRecord(at.UTC().Truncate(time.Microsecond))
	record.Subject = subject
	return record
}

func fullyPopulated(subject *decision.Subject, at time.Time) decision.Record {
	record := stored(subject, at)
	pod, binding, policy, address, rule := "test-pod-uid", "test-binding", "test-policy", "203.0.113.7", 3
	deny := decision.NoRule
	record.SourcePodUID, record.Binding, record.Policy, record.Address, record.Rule = &pod, &binding, &policy, &address, &rule
	record.Outcome, record.Reason, record.Substituted = decision.Deny, &deny, true
	return record
}

// equal compares the order and identity of records, and their instants whatever zone the database used.
func equal(t *testing.T, got, want []decision.Record) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("got %d records, want %d: %+v", len(got), len(want), got)
	}
	for i := range want {
		if got[i].EventID != want[i].EventID || !got[i].At.Equal(want[i].At) {
			t.Fatalf("record %d: got %+v, want %+v", i, got[i], want[i])
		}
	}
}

func TestSharedHistorySurvivesReplacementAndSeparatesSubjects(t *testing.T) {
	ctx := context.Background()
	subject, other := uniqueSubject(), uniqueSubject()
	now := time.Now()
	a, b := fullyPopulated(subject, now.Add(-2*time.Second)), stored(subject, now.Add(-time.Second))
	refusal := stored(nil, now)
	refusal.Outcome, refusal.Phase = decision.Deny, decision.Connect
	token := decision.TokenMissing
	refusal.Reason = &token

	// Each record is written by a replica that is gone before the next one starts.
	for _, record := range []decision.Record{a, b, refusal} {
		replica, err := NewLog(testStore(t, 200), testLogConfig())
		if err != nil {
			t.Fatal(err)
		}
		replica.Record(record)
		replica.Close()
		if health := replica.Health(); health.Acknowledged != 1 {
			t.Fatalf("not written: %+v", health)
		}
	}

	replacement := testStore(t, 200)
	got, err := replacement.Recent(ctx, subject)
	if err != nil {
		t.Fatal(err)
	}
	equal(t, got, []decision.Record{a, b})
	// Every field survives the round trip.
	if got[0].Subject == nil || *got[0].Subject != *subject || *got[0].Address != "203.0.113.7" || *got[0].Rule != 3 ||
		*got[0].Reason != decision.NoRule || !got[0].Substituted || *got[0].SourcePodUID != "test-pod-uid" ||
		*got[0].Binding != "test-binding" || *got[0].Policy != "test-policy" || got[1].Reason != nil || got[1].Address != nil {
		t.Fatalf("record did not round-trip: %+v", got[0])
	}
	if absent, err := replacement.Recent(ctx, &decision.Subject{Namespace: subject.Namespace, Name: "absent"}); err != nil || len(absent) != 0 {
		t.Fatalf("absent name: %v, %v", absent, err)
	}
	if elsewhere, err := replacement.Recent(ctx, other); err != nil || len(elsewhere) != 0 {
		t.Fatalf("the same name in another namespace is a different subject: %v, %v", elsewhere, err)
	}
	unauthenticated, err := replacement.Recent(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if i := indexOf(unauthenticated, refusal.EventID); i < 0 || unauthenticated[i].Subject != nil {
		t.Fatalf("refusal that never authenticated is missing or has a subject: %+v", unauthenticated)
	}
}

func indexOf(records []decision.Record, id uuid.UUID) int {
	for i, record := range records {
		if record.EventID == id {
			return i
		}
	}
	return -1
}

// flakyAcknowledgement commits a batch and then reports it lost, once.
type flakyAcknowledgement struct {
	*Store
	calls int
}

func (f *flakyAcknowledgement) Append(ctx context.Context, records []decision.Record) error {
	err := f.Store.Append(ctx, records)
	f.calls++
	if err == nil && f.calls == 1 {
		return errors.New("test acknowledgement lost")
	}
	return err
}

func TestDuplicateAcknowledgementAmbiguityStoresTheRecordOnce(t *testing.T) {
	store := testStore(t, 200)
	flaky := &flakyAcknowledgement{Store: store}
	l, err := newLog(flaky, retention, testLogConfig(), fastRetries)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(l.Close)
	subject := uniqueSubject()
	event := stored(subject, time.Now())

	l.Record(event)
	flush(t, l)

	got, err := store.Recent(context.Background(), subject)
	if err != nil {
		t.Fatal(err)
	}
	equal(t, got, []decision.Record{event})
	if health := l.Health(); flaky.calls != 2 || health.WriteFailures != 1 || health.Acknowledged != 1 {
		t.Fatalf("calls %d, health %+v", flaky.calls, health)
	}
}

func TestRetentionAndRecentLimit(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 2)
	subject := uniqueSubject()
	now := time.Now()
	expired := stored(subject, now.Add(-retention-24*time.Hour))
	current := []decision.Record{
		stored(subject, now.Add(-3*time.Second)), stored(subject, now.Add(-2*time.Second)), stored(subject, now.Add(-time.Second)),
	}
	if err := store.Append(ctx, append([]decision.Record{expired}, current...)); err != nil {
		t.Fatal(err)
	}

	got, err := store.Recent(ctx, subject)
	if err != nil {
		t.Fatal(err)
	}
	equal(t, got, current[1:])

	if err := store.Cleanup(ctx); err != nil {
		t.Fatal(err)
	}
	var rows int
	if err := store.db.QueryRow("SELECT count(*) FROM egress_decision WHERE subject_namespace = $1", subject.Namespace).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 3 {
		t.Fatalf("cleanup left %d rows of the subject's 4, want the 3 current", rows)
	}
}

func TestFollowWakesWhenAReplicaCommitsAndEndsWhenItsConnectionIsLost(t *testing.T) {
	// A heartbeat far past the test: only a commit can be what wakes the second frame.
	changes, err := NewChanges(integrationDatabase.URL, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	frames, ended := make(chan struct{}, 8), make(chan error, 1)
	go func() {
		ended <- changes.Follow(ctx, func() error {
			frames <- struct{}{}
			return nil
		})
	}()
	await := func(what string) {
		t.Helper()
		select {
		case <-frames:
		case err := <-ended:
			t.Fatalf("%s: stream ended: %v", what, err)
		case <-ctx.Done():
			t.Fatalf("%s: no frame", what)
		}
	}

	await("on subscribing")
	store := testStore(t, 200)
	if err := store.Append(ctx, []decision.Record{stored(uniqueSubject(), time.Now())}); err != nil {
		t.Fatal(err)
	}
	await("after a commit")

	if _, err := store.db.ExecContext(ctx,
		"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'agentplane-egress-decision-changes'"); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-ended:
		if err == nil || ctx.Err() != nil {
			t.Fatalf("Follow returned %v after %v", err, ctx.Err())
		}
	case <-ctx.Done():
		t.Fatal("stream outlived its LISTEN connection")
	}
}
