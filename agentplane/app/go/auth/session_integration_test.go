package auth

import (
	"context"
	"database/sql"
	"net/url"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	_ "github.com/lib/pq"
)

func integrationDB(t *testing.T) (*sessionStore, string) {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return &sessionStore{db: db, idle: time.Minute, step: 0, now: time.Now}, dsn
}

func integrationStore(t *testing.T) *sessionStore {
	store, _ := integrationDB(t)
	return store
}

func integrationHub(t *testing.T, dsn string) *wakeups.Hub {
	t.Helper()
	target, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	query := target.Query()
	query.Set("application_name", "agentplane-auth-test")
	target.RawQuery = query.Encode()
	hub, err := wakeups.Listen(t.Context(), target.String(), sessionNotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = hub.Close() })
	return hub
}

func awaitSignal(t *testing.T, signal <-chan struct{}) {
	t.Helper()
	select {
	case <-signal:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for operator session notification")
	}
}

func TestSessionPersistenceMergeLogoutAndNotifications(t *testing.T) {
	store, dsn := integrationDB(t)
	hub := integrationHub(t, dsn)
	signal, unsubscribe, err := hub.Subscribe(sessionNotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	defer unsubscribe()
	handle, err := store.create(t.Context(), map[string]any{"state": "pending"}, time.Now().UTC().Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.end(t.Context(), rowID(handle)); err != nil {
		t.Fatal(err)
	}
	awaitSignal(t, signal)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	ended := make(chan error, 1)
	go func() { ended <- store.untilEnded(ctx, rowID(handle), hub) }()
	select {
	case err := <-ended:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("UntilEnded did not observe the deleted session")
	}
}

func TestSessionPayloadMergeDoesNotResurrectLogout(t *testing.T) {
	store := integrationStore(t)
	absolute := time.Now().UTC().Add(time.Minute)
	handle, err := store.create(t.Context(), map[string]any{"first": "0", "second": "0"}, absolute)
	if err != nil {
		t.Fatal(err)
	}
	id := rowID(handle)
	first, err := store.read(t.Context(), id)
	if err != nil || first == nil {
		t.Fatalf("first read = (%+v, %v)", first, err)
	}
	second, err := store.read(t.Context(), id)
	if err != nil || second == nil {
		t.Fatalf("second read = (%+v, %v)", second, err)
	}
	if saved, err := store.save(t.Context(), id, first.payload, map[string]any{"first": "1", "second": "0"}); err != nil || !saved {
		t.Fatalf("first save = (%v, %v)", saved, err)
	}
	if saved, err := store.save(t.Context(), id, second.payload, map[string]any{"first": "0", "second": "1"}); err != nil || !saved {
		t.Fatalf("second save = (%v, %v)", saved, err)
	}
	latest, err := store.read(t.Context(), id)
	if err != nil || latest.payload["first"] != "1" || latest.payload["second"] != "1" {
		t.Fatalf("merged read = (%+v, %v)", latest, err)
	}
	if err := store.end(t.Context(), id); err != nil {
		t.Fatal(err)
	}
	if saved, err := store.save(t.Context(), id, latest.payload, map[string]any{"first": "resurrected"}); err != nil || saved {
		t.Fatalf("save after logout = (%v, %v), want false", saved, err)
	}
}

func TestLoginReplacementPersistsTypedIdentityAndWakesListeners(t *testing.T) {
	store, dsn := integrationDB(t)
	hub := integrationHub(t, dsn)
	signal, unsubscribe, err := hub.Subscribe(sessionNotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	defer unsubscribe()
	pending, err := store.create(t.Context(), map[string]any{"_state_authentik_test": map[string]any{"nonce": "n"}}, time.Now().UTC().Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	login := OperatorSession{Issuer: "https://issuer.test", Subject: "sub-1", Username: "operator-1", Tokens: &LoginTokens{AccessToken: "access", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh"}}
	handle, inserted, err := store.logIn(t.Context(), rowID(pending), map[string]any{}, login, time.Now().UTC().Add(time.Minute))
	if err != nil || !inserted {
		t.Fatalf("login replacement = (%q, %v, %v)", handle, inserted, err)
	}
	awaitSignal(t, signal)
	if row, err := store.read(t.Context(), rowID(pending)); err != nil || row != nil {
		t.Fatalf("pending row after callback = (%+v, %v)", row, err)
	}
	row, err := store.read(t.Context(), rowID(handle))
	if err != nil || row == nil || row.login == nil || row.login.Username != "operator-1" || row.login.Tokens == nil || row.login.Tokens.RefreshToken != "refresh" {
		t.Fatalf("logged-in row = (%+v, %v)", row, err)
	}
}

func TestExpiredRowIsDeletedAndNotified(t *testing.T) {
	store, dsn := integrationDB(t)
	hub := integrationHub(t, dsn)
	signal, unsubscribe, err := hub.Subscribe(sessionNotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	defer unsubscribe()
	handle, err := store.create(t.Context(), map[string]any{"state": "pending"}, time.Now().UTC().Add(-time.Second))
	if err != nil {
		t.Fatal(err)
	}
	id := rowID(handle)
	if row, err := store.read(t.Context(), id); err != nil || row != nil {
		t.Fatalf("expired read = (%+v, %v), want nil row", row, err)
	}
	awaitSignal(t, signal)
	var count int
	if err := store.db.QueryRowContext(t.Context(), `SELECT count(*) FROM operator_browser_session WHERE id=$1`, id).Scan(&count); err != nil || count != 0 {
		t.Fatalf("expired row count = (%d, %v)", count, err)
	}
}

func TestLockedLoginSerializesRefreshTokenRotation(t *testing.T) {
	store := integrationStore(t)
	login := OperatorSession{Issuer: "https://issuer.test", Subject: "subject", Username: "operator", Tokens: &LoginTokens{AccessToken: "access-0", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh-0"}}
	handle, inserted, err := store.logIn(t.Context(), "", map[string]any{}, login, time.Now().UTC().Add(time.Minute))
	if err != nil || !inserted {
		t.Fatalf("login insert = (%q, %v, %v)", handle, inserted, err)
	}
	row := &SessionRow{store: store, id: rowID(handle)}
	firstEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	secondSaw := make(chan string, 1)
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		err := row.WithLockedLogin(t.Context(), func(current *OperatorSession) (*OperatorSession, error) {
			close(firstEntered)
			<-releaseFirst
			updated := *current
			updated.Tokens = &LoginTokens{AccessToken: "access-1", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh-1"}
			return &updated, nil
		})
		if err != nil {
			t.Errorf("first locked refresh: %v", err)
		}
	}()
	<-firstEntered
	go func() {
		defer wg.Done()
		err := row.WithLockedLogin(t.Context(), func(current *OperatorSession) (*OperatorSession, error) {
			secondSaw <- current.Tokens.RefreshToken
			updated := *current
			updated.Tokens = &LoginTokens{AccessToken: "access-2", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh-2"}
			return &updated, nil
		})
		if err != nil {
			t.Errorf("second locked refresh: %v", err)
		}
	}()
	close(releaseFirst)
	wg.Wait()
	if got := <-secondSaw; got != "refresh-1" {
		t.Fatalf("second refresh saw %q, want rotated refresh-1", got)
	}
	latest, err := row.Login(t.Context())
	if err != nil || latest == nil || latest.Tokens.RefreshToken != "refresh-2" {
		t.Fatalf("final login = (%+v, %v)", latest, err)
	}
}
