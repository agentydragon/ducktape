package threads

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/migrations"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	database, err := postgres.Start(ctx, migrations.Up)
	cancel()
	if err != nil {
		fmt.Fprintln(os.Stderr, "start PostgreSQL fixture:", err)
		os.Exit(1)
	}
	integrationDatabase = database
	code := m.Run()
	if err := database.Close(); err != nil {
		fmt.Fprintln(os.Stderr, "close PostgreSQL fixture:", err)
		if code == 0 {
			code = 1
		}
	}
	os.Exit(code)
}

func openStore(t *testing.T) (*Store, *sql.DB, string) {
	t.Helper()
	url := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", url)
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(8)
	t.Cleanup(func() {
		if err := db.Close(); err != nil {
			t.Error(err)
		}
	})
	if err := db.PingContext(t.Context()); err != nil {
		t.Fatal(err)
	}
	return NewStore(db), db, url
}

func TestOpenRenameArchiveAndNotifications(t *testing.T) {
	store, _, url := openStore(t)
	hub, err := wakeups.Listen(t.Context(), url, UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = hub.Close() })
	wakeup, unsubscribe, err := hub.Subscribe(UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(unsubscribe)

	createdAt := time.Date(2026, 10, 23, 18, 0, 0, 0, time.UTC)
	store.now = func() time.Time { return createdAt }
	id := uuid.New()
	if err := store.OpenThread(t.Context(), id); err != nil {
		t.Fatal(err)
	}
	awaitWakeup(t, wakeup)
	if err := store.OpenThread(t.Context(), id); err != nil {
		t.Fatal(err)
	}
	assertQuiet(t, wakeup)

	thread, err := store.GetThread(t.Context(), id)
	if err != nil {
		t.Fatal(err)
	}
	if thread == nil {
		t.Fatal("opened thread is missing")
	}
	if thread.ID != id || !thread.CreatedAt.Equal(createdAt) || thread.Name != nil || thread.Archived {
		t.Fatalf("unexpected opened metadata: %+v", *thread)
	}
	if got, err := store.ArchivedCount(t.Context()); err != nil || got != 0 {
		t.Fatalf("ArchivedCount = %d, %v; want 0", got, err)
	}

	name := "Operator title"
	renamed, err := store.Rename(t.Context(), id, &name)
	if err != nil {
		t.Fatal(err)
	}
	awaitWakeup(t, wakeup)
	if renamed.Name == nil || *renamed.Name != name {
		t.Fatalf("unexpected renamed metadata: %+v", renamed)
	}
	archived, err := store.Archive(t.Context(), id)
	if err != nil {
		t.Fatal(err)
	}
	awaitWakeup(t, wakeup)
	if !archived.Archived {
		t.Fatalf("Archive returned %+v", archived)
	}
	if got, err := store.ArchivedCount(t.Context()); err != nil || got != 1 {
		t.Fatalf("ArchivedCount = %d, %v; want 1", got, err)
	}
	unarchived, err := store.Unarchive(t.Context(), id)
	if err != nil {
		t.Fatal(err)
	}
	awaitWakeup(t, wakeup)
	if unarchived.Archived {
		t.Fatalf("Unarchive returned %+v", unarchived)
	}

	if _, err := store.Rename(t.Context(), uuid.New(), nil); !errors.Is(err, ErrNotFound) {
		t.Fatalf("Rename of a missing thread = %v, want ErrNotFound", err)
	}
}

func TestListPageOrdersFiltersAndContinuesByCursor(t *testing.T) {
	store, _, _ := openStore(t)
	now := time.Date(2026, 10, 23, 18, 0, 0, 0, time.UTC)
	store.now = func() time.Time { return now }
	ids := []uuid.UUID{
		uuid.MustParse("00000000-0000-0000-0000-000000000001"),
		uuid.MustParse("00000000-0000-0000-0000-000000000002"),
		uuid.MustParse("00000000-0000-0000-0000-000000000003"),
	}
	for _, id := range ids {
		if err := store.OpenThread(t.Context(), id); err != nil {
			t.Fatal(err)
		}
		now = now.Add(time.Minute)
	}
	if _, err := store.Archive(t.Context(), ids[1]); err != nil {
		t.Fatal(err)
	}

	page, err := store.ListPage(t.Context(), 1, nil, false)
	if err != nil || len(page.Threads) != 1 || page.Threads[0].ID != ids[2] || page.Next == nil {
		t.Fatalf("first default page = %+v, %v", page, err)
	}
	page, err = store.ListPage(t.Context(), 1, page.Next, false)
	if err != nil || len(page.Threads) != 1 || page.Threads[0].ID != ids[0] || page.Next != nil {
		t.Fatalf("second default page = %+v, %v", page, err)
	}

	page, err = store.ListPage(t.Context(), 2, nil, true)
	if err != nil || len(page.Threads) != 2 || page.Threads[0].ID != ids[2] ||
		page.Threads[1].ID != ids[1] || page.Next == nil {
		t.Fatalf("first inclusive page = %+v, %v", page, err)
	}
	page, err = store.ListPage(t.Context(), 2, page.Next, true)
	if err != nil || len(page.Threads) != 1 || page.Threads[0].ID != ids[0] || page.Next != nil {
		t.Fatalf("second inclusive page = %+v, %v", page, err)
	}
	if _, err := store.ListPage(t.Context(), 0, nil, false); err == nil {
		t.Fatal("ListPage accepted a zero page size")
	}
}

func awaitWakeup(t *testing.T, wakeup <-chan struct{}) {
	t.Helper()
	select {
	case <-wakeup:
	case <-time.After(10 * time.Second):
		t.Fatal("thread metadata write did not notify subscribers")
	}
}

func assertQuiet(t *testing.T, wakeup <-chan struct{}) {
	t.Helper()
	select {
	case <-wakeup:
		t.Fatal("thread metadata operation emitted an unexpected notification")
	case <-time.After(100 * time.Millisecond):
	}
}
