package wakeups

import (
	"context"
	"database/sql"
	"net/url"
	"testing"
	"time"
)

// awaitBound only fails a test whose wakeup never comes.
const awaitBound = 30 * time.Second

func openDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

// listen returns a hub whose LISTEN connection is called `name` in pg_stat_activity.
func listen(t *testing.T, name string, channels ...string) *Hub {
	t.Helper()
	target, err := url.Parse(integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	query := target.Query()
	query.Set("application_name", name)
	target.RawQuery = query.Encode()
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
	db, hub := openDB(t), listen(t, "test-wakeups-unsubscribe", "test_wakeups_end")
	ended, unsubscribe := subscribe(t, hub, "test_wakeups_end")
	kept, _ := subscribe(t, hub, "test_wakeups_end")
	unsubscribe()
	notify(t, db, "test_wakeups_end")
	awaitWakeup(t, kept)
	assertQuiet(t, ended)
}

func TestSubscribingToAChannelNobodyListensToIsRefused(t *testing.T) {
	hub := listen(t, "test-wakeups-unknown", "test_wakeups_known")
	if _, _, err := hub.Subscribe("test_wakeups_known", "test_wakeups_unknown"); err == nil {
		t.Fatal("subscribed to a channel the hub does not listen to")
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
