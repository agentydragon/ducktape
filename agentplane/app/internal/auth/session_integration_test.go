package auth

import (
	"context"
	"database/sql"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth/session"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

func integrationDB(t *testing.T) (*session.Store, *sql.DB, string) {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return session.NewStore(db, time.Minute, 0), db, dsn
}

func integrationStore(t *testing.T) *session.Store {
	store, _, _ := integrationDB(t)
	return store
}

func integrationHub(t *testing.T, dsn string) *wakeups.Hub {
	t.Helper()
	target := postgres.URLWithApplicationName(t, dsn, "agentplane-auth-test")
	hub, err := wakeups.Listen(t.Context(), target.String(), session.SessionUpdatesChannel)
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
	store, _, dsn := integrationDB(t)
	hub := integrationHub(t, dsn)
	signal, unsubscribe, err := hub.Subscribe(session.SessionUpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	defer unsubscribe()
	handle, err := store.Create(t.Context(), map[string]session.Attempt{"state": {Nonce: "pending"}}, time.Now().UTC().Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Terminate(t.Context(), session.IDForHandle(handle)); err != nil {
		t.Fatal(err)
	}
	awaitSignal(t, signal)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	ended := make(chan error, 1)
	go func() { ended <- store.UntilTerminated(ctx, session.IDForHandle(handle), hub) }()
	select {
	case err := <-ended:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("UntilEnded did not observe the deleted session")
	}
}

func TestLoginAttemptMergeDoesNotResurrectLogout(t *testing.T) {
	store := integrationStore(t)
	absolute := time.Now().UTC().Add(time.Minute)
	handle, err := store.Create(t.Context(), map[string]session.Attempt{"first": {Nonce: "0"}, "second": {Nonce: "0"}}, absolute)
	if err != nil {
		t.Fatal(err)
	}
	id := session.IDForHandle(handle)
	first, err := store.Read(t.Context(), id)
	if err != nil || first == nil {
		t.Fatalf("first read = (%+v, %v)", first, err)
	}
	second, err := store.Read(t.Context(), id)
	if err != nil || second == nil {
		t.Fatalf("second read = (%+v, %v)", second, err)
	}
	if saved, err := store.SavePending(t.Context(), id, first.PendingLogins, map[string]session.Attempt{"first": {Nonce: "1"}, "second": {Nonce: "0"}}); err != nil || !saved {
		t.Fatalf("first save = (%v, %v)", saved, err)
	}
	if saved, err := store.SavePending(t.Context(), id, second.PendingLogins, map[string]session.Attempt{"first": {Nonce: "0"}, "second": {Nonce: "1"}}); err != nil || !saved {
		t.Fatalf("second save = (%v, %v)", saved, err)
	}
	latest, err := store.Read(t.Context(), id)
	if err != nil || latest == nil || latest.PendingLogins["first"].Nonce != "1" || latest.PendingLogins["second"].Nonce != "1" {
		t.Fatalf("merged read = (%+v, %v)", latest, err)
	}
	if err := store.Terminate(t.Context(), id); err != nil {
		t.Fatal(err)
	}
	if saved, err := store.SavePending(t.Context(), id, latest.PendingLogins, map[string]session.Attempt{"first": {Nonce: "resurrected"}}); err != nil || saved {
		t.Fatalf("save after logout = (%v, %v), want false", saved, err)
	}
}

func TestLoginReplacementPersistsTypedIdentityAndWakesListeners(t *testing.T) {
	store, _, dsn := integrationDB(t)
	hub := integrationHub(t, dsn)
	signal, unsubscribe, err := hub.Subscribe(session.SessionUpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	defer unsubscribe()
	attempt := session.Attempt{Nonce: "n", CodeVerifier: "verifier", ExpiresAt: time.Now().UTC().Add(time.Minute)}
	pending, err := store.Create(t.Context(), map[string]session.Attempt{"test-state": attempt}, time.Now().UTC().Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	persisted, err := store.Read(t.Context(), session.IDForHandle(pending))
	if err != nil || persisted == nil || persisted.PendingLogins["test-state"] != attempt {
		t.Fatalf("pending login round trip = (%+v, %v)", persisted, err)
	}
	login := session.Login{Issuer: "https://issuer.test", Subject: "sub-1", Username: "operator-1", Tokens: &session.Tokens{AccessToken: "access", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh"}}
	handle, inserted, err := store.CompleteLogin(t.Context(), session.IDForHandle(pending), map[string]session.Attempt{}, login, time.Now().UTC().Add(time.Minute))
	if err != nil || !inserted {
		t.Fatalf("login replacement = (%q, %v, %v)", handle, inserted, err)
	}
	awaitSignal(t, signal)
	if row, err := store.Read(t.Context(), session.IDForHandle(pending)); err != nil || row != nil {
		t.Fatalf("pending row after callback = (%+v, %v)", row, err)
	}
	row, err := store.Read(t.Context(), session.IDForHandle(handle))
	if err != nil || row == nil || row.Login == nil || row.Login.Username != "operator-1" || row.Login.Tokens == nil || row.Login.Tokens.RefreshToken != "refresh" {
		t.Fatalf("logged-in row = (%+v, %v)", row, err)
	}
}

func TestExpiredRowIsDeletedAndNotified(t *testing.T) {
	store, db, dsn := integrationDB(t)
	hub := integrationHub(t, dsn)
	signal, unsubscribe, err := hub.Subscribe(session.SessionUpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	defer unsubscribe()
	handle, err := store.Create(t.Context(), map[string]session.Attempt{"state": {Nonce: "pending"}}, time.Now().UTC().Add(-time.Second))
	if err != nil {
		t.Fatal(err)
	}
	id := session.IDForHandle(handle)
	if row, err := store.Read(t.Context(), id); err != nil || row != nil {
		t.Fatalf("expired read = (%+v, %v), want nil row", row, err)
	}
	awaitSignal(t, signal)
	var count int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM operator_browser_session WHERE id=$1`, string(id)).Scan(&count); err != nil || count != 0 {
		t.Fatalf("expired row count = (%d, %v)", count, err)
	}
}

func TestLockedLoginSerializesRefreshTokenRotation(t *testing.T) {
	store := integrationStore(t)
	login := session.Login{Issuer: "https://issuer.test", Subject: "subject", Username: "operator", Tokens: &session.Tokens{AccessToken: "access-0", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh-0"}}
	handle, inserted, err := store.CompleteLogin(t.Context(), "", map[string]session.Attempt{}, login, time.Now().UTC().Add(time.Minute))
	if err != nil || !inserted {
		t.Fatalf("login insert = (%q, %v, %v)", handle, inserted, err)
	}
	id := session.IDForHandle(handle)
	firstEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	secondSaw := make(chan string, 1)
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		err := store.RefreshLogin(t.Context(), id, func(current *session.Login) (*session.Login, error) {
			close(firstEntered)
			<-releaseFirst
			updated := *current
			updated.Tokens = &session.Tokens{AccessToken: "access-1", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh-1"}
			return &updated, nil
		})
		if err != nil {
			t.Errorf("first locked refresh: %v", err)
		}
	}()
	<-firstEntered
	go func() {
		defer wg.Done()
		err := store.RefreshLogin(t.Context(), id, func(current *session.Login) (*session.Login, error) {
			secondSaw <- current.Tokens.RefreshToken
			updated := *current
			updated.Tokens = &session.Tokens{AccessToken: "access-2", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh-2"}
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
	latest, err := store.Read(t.Context(), id)
	if err != nil || latest == nil || latest.Login.Tokens.RefreshToken != "refresh-2" {
		t.Fatalf("final login = (%+v, %v)", latest, err)
	}
}

func TestTerminationSerializesWithRefresh(t *testing.T) {
	store := integrationStore(t)
	login := session.Login{
		Issuer: "https://issuer.test", Subject: "subject", Username: "operator",
		Tokens: &session.Tokens{AccessToken: "access-0", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh-0"},
	}
	handle, inserted, err := store.CompleteLogin(t.Context(), "", map[string]session.Attempt{}, login, time.Now().UTC().Add(time.Minute))
	if err != nil || !inserted {
		t.Fatalf("login insert = (%q, %v, %v)", handle, inserted, err)
	}
	id := session.IDForHandle(handle)
	refreshEntered := make(chan struct{})
	releaseRefresh := make(chan struct{})
	refreshDone := make(chan error, 1)
	go func() {
		refreshDone <- store.RefreshLogin(t.Context(), id, func(current *session.Login) (*session.Login, error) {
			close(refreshEntered)
			<-releaseRefresh
			updated := *current
			updated.Tokens = &session.Tokens{AccessToken: "access-1", ExpiresAt: time.Now().Add(time.Minute), RefreshToken: "refresh-1"}
			return &updated, nil
		})
	}()
	<-refreshEntered
	terminationDone := make(chan error, 1)
	go func() { terminationDone <- store.Terminate(t.Context(), id) }()
	close(releaseRefresh)
	if err := <-refreshDone; err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if err := <-terminationDone; err != nil {
		t.Fatalf("termination: %v", err)
	}
	if row, err := store.Read(t.Context(), id); err != nil || row != nil {
		t.Fatalf("row after refresh and termination = (%+v, %v), want no row", row, err)
	}
	callbackCalled := false
	if err := store.RefreshLogin(t.Context(), id, func(current *session.Login) (*session.Login, error) {
		callbackCalled = true
		return current, nil
	}); err != nil {
		t.Fatalf("refresh after termination: %v", err)
	}
	if callbackCalled {
		t.Fatal("refresh callback ran after termination")
	}
}
