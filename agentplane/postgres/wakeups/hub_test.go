package wakeups

import (
	"context"
	"database/sql"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/lib/pq"
)

// awaitBound only fails a test whose wakeup never comes.
const awaitBound = 30 * time.Second

func openDB(t *testing.T) *sql.DB {
	t.Helper()
	db := postgres.Open(t, integrationDatabase.URL)
	return db
}

// listen returns a hub whose LISTEN connection is called `name` in pg_stat_activity.
func listen(t *testing.T, name string, channels ...string) *Hub {
	t.Helper()
	target := postgres.URLWithApplicationName(t, integrationDatabase.URL, name)
	hub, err := Listen(t.Context(), target.String(), channels...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	return hub
}

func subscribe(t *testing.T, hub *Hub, channels ...string) (<-chan struct{}, func()) {
	t.Helper()
	signal, unsubscribe, err := hub.Subscribe(channels...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(unsubscribe)
	return signal, unsubscribe
}

func notify(t *testing.T, db *sql.DB, channel string) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), "SELECT pg_notify($1, '')", channel); err != nil {
		t.Fatal(err)
	}
}

func awaitWakeup(t *testing.T, signal <-chan struct{}) {
	t.Helper()
	select {
	case <-signal:
	case <-time.After(awaitBound):
		t.Fatal("no wakeup")
	}
}

func assertQuiet(t *testing.T, signal <-chan struct{}) {
	t.Helper()
	select {
	case <-signal:
		t.Fatal("unexpected wakeup")
	default:
	}
}

// The hub applies notifications in the order PostgreSQL delivers them, so once a wakeup caused by a later
// notification has arrived, every effect of the earlier ones has happened.

func TestANotificationWakesOnlyTheSubscribersOfItsChannel(t *testing.T) {
	db, hub := openDB(t), listen(t, "test-wakeups-routing", "test_wakeups_a", "test_wakeups_b")
	onA, _ := subscribe(t, hub, "test_wakeups_a")
	onB, _ := subscribe(t, hub, "test_wakeups_b")
	onBoth, _ := subscribe(t, hub, "test_wakeups_a", "test_wakeups_b")
	notify(t, db, "test_wakeups_a")
	awaitWakeup(t, onA)
	awaitWakeup(t, onBoth)
	notify(t, db, "test_wakeups_a")
	awaitWakeup(t, onA)
	awaitWakeup(t, onBoth)
	assertQuiet(t, onB)
	notify(t, db, "test_wakeups_b")
	awaitWakeup(t, onB)
	awaitWakeup(t, onBoth)
}

func TestABurstOfNotificationsLeavesOneWakeup(t *testing.T) {
	db, hub := openDB(t), listen(t, "test-wakeups-burst", "test_wakeups_burst", "test_wakeups_after")
	burst, _ := subscribe(t, hub, "test_wakeups_burst")
	after, _ := subscribe(t, hub, "test_wakeups_after")
	for range 5 {
		notify(t, db, "test_wakeups_burst")
	}
	notify(t, db, "test_wakeups_after")
	awaitWakeup(t, after)
	awaitWakeup(t, burst)
	assertQuiet(t, burst)
}

func TestAnEndedSubscriptionIsNotWoken(t *testing.T) {
	db, hub := openDB(t), listen(t, "test-wakeups-unsubscribe", "test_wakeups_end", "test_wakeups_barrier")
	channels := []string{"test_wakeups_end"}
	ended, unsubscribe := subscribe(t, hub, channels...)
	// The caller may reuse the slice after subscribing.
	channels[0] = "another_channel"
	kept, _ := subscribe(t, hub, "test_wakeups_end")
	barrier, _ := subscribe(t, hub, "test_wakeups_barrier")
	unsubscribe()
	notify(t, db, "test_wakeups_end")
	notify(t, db, "test_wakeups_barrier")
	awaitWakeup(t, barrier)
	awaitWakeup(t, kept)
	assertQuiet(t, ended)
}

func TestSubscribingToAChannelNobodyListensToIsRefused(t *testing.T) {
	hub := listen(t, "test-wakeups-unknown", "test_wakeups_known")
	if _, _, err := hub.Subscribe("test_wakeups_known", "test_wakeups_unknown"); err == nil {
		t.Fatal("subscribed to a channel the hub does not listen to")
	}
}

func TestDisconnectWakesSubscribersWithTheDisconnectedState(t *testing.T) {
	hub := newHub("updates")
	onUpdates, _ := subscribe(t, hub, "updates")
	onKey, stopKey, err := hub.SubscribeKey("updates", "a")
	if err != nil {
		t.Fatal(err)
	}
	defer stopKey()
	stopPayloads, err := hub.SubscribePayload("updates", func(key string) {
		t.Errorf("disconnect delivered payload %q", key)
	})
	if err != nil {
		t.Fatal(err)
	}
	defer stopPayloads()
	hub.connected.Store(true)
	hub.observeConnectionEvent(pq.ListenerEventDisconnected)
	awaitWakeup(t, onUpdates)
	awaitWakeup(t, onKey)
	if hub.Connected() {
		t.Fatal("disconnect wakeup must expose the disconnected state")
	}
}

func TestEverySubscriberWakesAfterTheConnectionIsLostAndListeningContinues(t *testing.T) {
	db, hub := openDB(t), listen(t, "test-wakeups-reconnect", "test_wakeups_a", "test_wakeups_b")
	onA, _ := subscribe(t, hub, "test_wakeups_a")
	onB, _ := subscribe(t, hub, "test_wakeups_b")
	if _, err := db.ExecContext(t.Context(),
		"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'test-wakeups-reconnect'"); err != nil {
		t.Fatal(err)
	}
	awaitWakeup(t, onA)
	awaitWakeup(t, onB)
	// Signals coalesce: the subscriber can resume after reconnection has already completed.
	for !hub.Connected() {
		awaitWakeup(t, onA)
		awaitWakeup(t, onB)
	}
	// The listener registered its channels again on the new connection.
	notify(t, db, "test_wakeups_a")
	awaitWakeup(t, onA)
}

func TestPingSucceedsOnALiveConnectionAndFailsOnceTheHubIsClosed(t *testing.T) {
	hub := listen(t, "test-wakeups-ping", "test_wakeups_a")
	if !hub.Connected() {
		t.Fatal("a listening hub reports disconnected")
	}
	if err := hub.Ping(); err != nil {
		t.Fatal(err)
	}
	if err := hub.Close(); err != nil {
		t.Fatal(err)
	}
	if hub.Ping() == nil {
		t.Fatal("a closed hub answered a ping")
	}
}

func TestListeningFailsWhenTheDatabaseCannotBeReached(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	// Port 1 refuses connections.
	if _, err := Listen(ctx, "postgres://test-user@127.0.0.1:1/test-database?sslmode=disable&connect_timeout=1", "test_wakeups_a"); err == nil {
		t.Fatal("listening succeeded without a database")
	}
}

func TestKeyedSubscribersReceiveOnlyTheirCommittedKeys(t *testing.T) {
	db, hub := openDB(t), listen(t, "test-keyed-wakeups", "test_keyed", "test_keyed_barrier")
	a, stopA, err := hub.SubscribeKey("test_keyed", "a")
	if err != nil {
		t.Fatal(err)
	}
	defer stopA()
	b, stopB, err := hub.SubscribeKey("test_keyed", "b")
	if err != nil {
		t.Fatal(err)
	}
	defer stopB()
	barrier, _ := subscribe(t, hub, "test_keyed_barrier")
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.ExecContext(t.Context(), `SELECT pg_notify('test_keyed', 'b')`); err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `SELECT pg_notify('test_keyed', 'a')`); err != nil {
		t.Fatal(err)
	}
	notify(t, db, "test_keyed_barrier")
	awaitWakeup(t, barrier)
	awaitWakeup(t, a)
	assertQuiet(t, b)
	if _, err := db.ExecContext(t.Context(), `SELECT pg_notify('test_keyed', 'b')`); err != nil {
		t.Fatal(err)
	}
	awaitWakeup(t, b)
	assertQuiet(t, a)
}

func TestPayloadSubscriptionHandsOffEveryDistinctCommittedKey(t *testing.T) {
	db, hub := openDB(t), listen(t, "test-payload-handoff", "test_payloads")
	var mu sync.Mutex
	seen := map[string]bool{}
	done := make(chan struct{})
	stop, err := hub.SubscribePayload("test_payloads", func(key string) {
		mu.Lock()
		defer mu.Unlock()
		seen[key] = true
		if key == "test-barrier" {
			close(done)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := range 100 {
		if _, err := tx.ExecContext(t.Context(), "SELECT pg_notify('test_payloads',$1)", fmt.Sprint(i)); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := tx.ExecContext(t.Context(), "SELECT pg_notify('test_payloads','test-barrier')"); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(awaitBound):
		t.Fatal("payloads did not arrive")
	}
	mu.Lock()
	defer mu.Unlock()
	for i := range 100 {
		if !seen[fmt.Sprint(i)] {
			t.Fatalf("lost key %d", i)
		}
	}
}
