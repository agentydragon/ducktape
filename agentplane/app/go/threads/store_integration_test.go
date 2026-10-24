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
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
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
	if err := store.OpenThread(t.Context(), id, true); err != nil {
		t.Fatal(err)
	}
	awaitWakeup(t, wakeup)
	if err := store.OpenThread(t.Context(), id, false); err != nil {
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
	if thread.ID != id || !thread.CreatedAt.Equal(createdAt) || thread.Name != nil || thread.Archived ||
		!thread.AutoTitleOnFirstMessage || thread.TitleUserManaged || thread.TitleStatus != "waiting" ||
		thread.TitleScanCursor != 0 || thread.TitleClaimedAt != nil || thread.TitleRevision != 0 {
		t.Fatalf("unexpected opened metadata: %+v", *thread)
	}
	if got, err := store.ArchivedCount(t.Context()); err != nil || got != 0 {
		t.Fatalf("ArchivedCount = %d, %v; want 0", got, err)
	}

	if err := store.AdvanceTitleScanCursor(t.Context(), id, 7); err != nil {
		t.Fatal(err)
	}
	assertQuiet(t, wakeup)
	thread, err = store.GetThread(t.Context(), id)
	if err != nil || thread == nil || thread.TitleScanCursor != 7 {
		t.Fatalf("cursor advance = %+v, %v", thread, err)
	}

	name := "Operator title"
	renamed, err := store.Rename(t.Context(), id, &name)
	if err != nil {
		t.Fatal(err)
	}
	awaitWakeup(t, wakeup)
	if renamed.Name == nil || *renamed.Name != name || !renamed.TitleUserManaged ||
		renamed.TitleStatus != "done" || renamed.TitleClaimedAt != nil || renamed.TitleRevision != 1 {
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

func TestAutoTitleClaimRecoveryAndOptimisticRevision(t *testing.T) {
	store, _, _ := openStore(t)
	now := time.Date(2026, 10, 23, 18, 0, 0, 0, time.UTC)
	store.now = func() time.Time { return now }
	id := uuid.New()
	if err := store.OpenThread(t.Context(), id, true); err != nil {
		t.Fatal(err)
	}
	pending, err := store.PendingAutoTitles(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 1 || pending[0] != (PendingTitle{ID: id, Cursor: 0}) {
		t.Fatalf("initial pending titles = %+v", pending)
	}

	revision, err := store.ClaimAutoTitle(t.Context(), id, 8)
	if err != nil || revision == nil || *revision != 0 {
		t.Fatalf("ClaimAutoTitle = %v, %v; want revision 0", revision, err)
	}
	if again, err := store.ClaimAutoTitle(t.Context(), id, 9); err != nil || again != nil {
		t.Fatalf("second ClaimAutoTitle = %v, %v; want no claim", again, err)
	}
	pending, err = store.PendingAutoTitles(t.Context())
	if err != nil || len(pending) != 0 {
		t.Fatalf("fresh claim pending titles = %+v, %v; want none", pending, err)
	}

	now = now.Add(6 * time.Minute)
	pending, err = store.PendingAutoTitles(t.Context())
	if err != nil || len(pending) != 1 || pending[0] != (PendingTitle{ID: id, Cursor: 8}) {
		t.Fatalf("stale pending titles = %+v, %v", pending, err)
	}
	revision, err = store.ClaimAutoTitle(t.Context(), id, 11)
	if err != nil || revision == nil || *revision != 0 {
		t.Fatalf("reclaimed title = %v, %v; want revision 0", revision, err)
	}
	applied, err := store.ApplyFirstMessageTitle(t.Context(), id, "Generated first title", *revision)
	if err != nil || !applied {
		t.Fatalf("ApplyFirstMessageTitle = %v, %v; want true", applied, err)
	}
	applied, err = store.ApplyFirstMessageTitle(t.Context(), id, "late result", *revision)
	if err != nil || applied {
		t.Fatalf("duplicate ApplyFirstMessageTitle = %v, %v; want false", applied, err)
	}
	thread, err := store.GetThread(t.Context(), id)
	if err != nil || thread == nil {
		t.Fatalf("GetThread after title = %+v, %v", thread, err)
	}
	if thread.Name == nil || *thread.Name != "Generated first title" ||
		thread.TitleStatus != "done" || thread.TitleClaimedAt != nil || thread.TitleScanCursor != 11 ||
		thread.TitleRevision != 1 || thread.TitleUserManaged {
		t.Fatalf("unexpected generated metadata: %+v", *thread)
	}
	if _, err := store.GeneratedTitle(t.Context(), id, "stale generated title", 0); !errors.Is(err, ErrConcurrentTitleUpdate) {
		t.Fatalf("GeneratedTitle with stale revision = %v, want ErrConcurrentTitleUpdate", err)
	}

	manualName := "Manual title"
	manual, err := store.Rename(t.Context(), id, &manualName)
	if err != nil {
		t.Fatal(err)
	}
	if manual.TitleRevision != 2 || !manual.TitleUserManaged {
		t.Fatalf("manual rename did not advance title ownership: %+v", manual)
	}
	if got, err := store.ClaimAutoTitle(t.Context(), id, 12); err != nil || got != nil {
		t.Fatalf("ClaimAutoTitle after manual rename = %v, %v; want no claim", got, err)
	}
	titleRevision, titleName, err := store.TitleContext(t.Context(), id)
	if err != nil || titleRevision != 2 || titleName == nil || *titleName != manualName {
		t.Fatalf("TitleContext = (%d, %v, %v)", titleRevision, titleName, err)
	}
	if _, _, err := store.TitleContext(t.Context(), uuid.New()); !errors.Is(err, ErrNotFound) {
		t.Fatalf("TitleContext for missing thread = %v, want ErrNotFound", err)
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
		if err := store.OpenThread(t.Context(), id, false); err != nil {
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
