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
	record.Impersonated = &decision.Subject{Namespace: "test-accounts", Name: "test-account"}
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
	if err := replacement.Readable(ctx); err != nil {
		t.Fatalf("history health read failed: %v", err)
	}
	got, err := replacement.Recent(ctx, subject)
	if err != nil {
		t.Fatal(err)
	}
	equal(t, got, []decision.Record{a, b})
	// Every field survives the round trip.
	if got[0].Subject == nil || *got[0].Subject != *subject || *got[0].Address != "203.0.113.7" || *got[0].Rule != 3 ||
		*got[0].Reason != decision.NoRule || !got[0].Substituted || *got[0].SourcePodUID != "test-pod-uid" ||
		*got[0].Binding != "test-binding" || *got[0].Policy != "test-policy" ||
		got[0].Impersonated == nil || *got[0].Impersonated != *a.Impersonated ||
		got[1].Reason != nil || got[1].Address != nil || got[1].Impersonated != nil {
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

func TestFollowQueuesCommitThatRacesTheInitialSnapshot(t *testing.T) {
	changes, err := NewChanges(integrationDatabase.URL, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	subject := uniqueSubject()
	store := testStore(t, 200)
	initialDone := make(chan struct{})
	continueInitial := make(chan struct{})
	updates := make(chan Change, 1)
	ended := make(chan error, 1)
	go func() {
		ended <- changes.Follow(ctx, subject, func(change Change) error {
			switch change := change.(type) {
			case Initial:
				// This is the same read the admin window performs. A commit after this query is
				// queued by LISTEN until the callback returns, rather than lost at the cut.
				records, err := store.Recent(ctx, subject)
				if err != nil {
					return err
				}
				if len(records) != 0 {
					return errors.New("unexpected record in initial snapshot")
				}
				close(initialDone)
				select {
				case <-continueInitial:
					return nil
				case <-ctx.Done():
					return ctx.Err()
				}
			default:
				updates <- change
			}
			return nil
		})
	}()
	select {
	case <-initialDone:
	case <-ctx.Done():
		t.Fatal("initial snapshot did not run")
	}
	record := stored(subject, time.Now())
	if err := store.Append(ctx, []decision.Record{record}); err != nil {
		t.Fatal(err)
	}
	close(continueInitial)
	select {
	case change := <-updates:
		events, ok := change.(Events)
		if !ok || len(events.EventIDs) != 1 || events.EventIDs[0] != record.EventID {
			t.Fatalf("change after snapshot = %#v, want committed event ID", change)
		}
	case err := <-ended:
		t.Fatalf("feed ended before queued event: %v", err)
	case <-ctx.Done():
		t.Fatal("commit racing initial snapshot was not delivered")
	}
	cancel()
	select {
	case <-ended:
	case <-time.After(5 * time.Second):
		t.Fatal("follower did not end after cancellation")
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
	// A heartbeat far past the test: only a commit can wake the subscribed subject.
	changes, err := NewChanges(integrationDatabase.URL, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	subject, unrelated := uniqueSubject(), uniqueSubject()
	type followResult struct {
		change Change
		err    error
	}
	start := func() (<-chan followResult, <-chan error) {
		frames, ended := make(chan followResult, 32), make(chan error, 1)
		go func() {
			ended <- changes.Follow(ctx, subject, func(change Change) error {
				frames <- followResult{change: change}
				return nil
			})
		}()
		return frames, ended
	}
	firstFrames, firstEnded := start()
	secondFrames, secondEnded := start()
	await := func(frames <-chan followResult, ended <-chan error, what string) Change {
		t.Helper()
		select {
		case result := <-frames:
			return result.change
		case err := <-ended:
			t.Fatalf("%s: stream ended: %v", what, err)
		case <-ctx.Done():
			t.Fatalf("%s: no frame", what)
		}
		return nil
	}

	if _, ok := await(firstFrames, firstEnded, "first subscription").(Initial); !ok {
		t.Fatal("first subscription did not receive Initial")
	}
	if _, ok := await(secondFrames, secondEnded, "second subscription").(Initial); !ok {
		t.Fatal("second subscription did not receive Initial")
	}
	store := testStore(t, 200)
	var listenerCount int
	if err := store.db.QueryRowContext(ctx,
		"SELECT count(*) FROM pg_stat_activity WHERE application_name = 'agentplane-egress-decision-changes'").Scan(&listenerCount); err != nil {
		t.Fatal(err)
	}
	if listenerCount != 1 {
		t.Fatalf("two followers opened %d LISTEN connections, want one", listenerCount)
	}
	otherRecord := stored(unrelated, time.Now())
	if err := store.Append(ctx, []decision.Record{otherRecord}); err != nil {
		t.Fatal(err)
	}
	for _, frames := range []<-chan followResult{firstFrames, secondFrames} {
		select {
		case update := <-frames:
			t.Fatalf("unrelated subject produced update %T", update.change)
		case <-time.After(150 * time.Millisecond):
		}
	}
	record := stored(subject, time.Now())
	if err := store.Append(ctx, []decision.Record{record}); err != nil {
		t.Fatal(err)
	}
	for i, pair := range []struct {
		frames <-chan followResult
		ended  <-chan error
	}{{firstFrames, firstEnded}, {secondFrames, secondEnded}} {
		update, ok := await(pair.frames, pair.ended, "after a commit").(Events)
		if !ok || len(update.EventIDs) != 1 || update.EventIDs[0] != record.EventID {
			t.Fatalf("follower %d got update %#v, want its committed event ID", i, update)
		}
	}

	if _, err := store.db.ExecContext(ctx,
		"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'agentplane-egress-decision-changes'"); err != nil {
		t.Fatal(err)
	}
	for i, ended := range []<-chan error{firstEnded, secondEnded} {
		select {
		case err := <-ended:
			if err == nil || ctx.Err() != nil {
				t.Fatalf("follower %d returned %v after %v", i, err, ctx.Err())
			}
		case <-ctx.Done():
			t.Fatal("stream outlived its LISTEN connection")
		}
	}

	resumedFrames := make(chan Change, 2)
	resumedEnded := make(chan error, 1)
	go func() {
		resumedEnded <- changes.Follow(ctx, subject, func(change Change) error {
			if _, ok := change.(Initial); ok {
				records, err := store.Recent(ctx, subject)
				if err != nil {
					return err
				}
				if indexOf(records, record.EventID) < 0 {
					return errors.New("reconnected feed snapshot omitted the committed record")
				}
			}
			resumedFrames <- change
			return nil
		})
	}()
	select {
	case change := <-resumedFrames:
		if _, ok := change.(Initial); !ok {
			t.Fatalf("reconnected feed began with %T, want Initial", change)
		}
	case err := <-resumedEnded:
		t.Fatalf("reconnected feed ended before its snapshot: %v", err)
	case <-ctx.Done():
		t.Fatal("reconnected feed did not produce a new snapshot")
	}
	resumedRecord := stored(subject, time.Now())
	if err := store.Append(ctx, []decision.Record{resumedRecord}); err != nil {
		t.Fatal(err)
	}
	select {
	case change := <-resumedFrames:
		update, ok := change.(Events)
		if !ok || len(update.EventIDs) != 1 || update.EventIDs[0] != resumedRecord.EventID {
			t.Fatalf("reconnected feed update = %#v, want new committed event", change)
		}
	case err := <-resumedEnded:
		t.Fatalf("reconnected feed ended before its new commit: %v", err)
	case <-ctx.Done():
		t.Fatal("reconnected feed did not receive its new commit")
	}
	cancel()
	select {
	case <-resumedEnded:
	case <-time.After(5 * time.Second):
		t.Fatal("reconnected feed did not end on cancellation")
	}
}
