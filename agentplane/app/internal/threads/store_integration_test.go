package threads

import (
	"database/sql"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/internal/migrations"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(migrations.Up, func(database *postgres.Database) int {
		integrationDatabase = database
		return m.Run()
	}))
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
}

func TestFirstWriteCreatesMetadataAndPreservesOtherFields(t *testing.T) {
	for _, first := range []string{"rename", "archive", "unarchive"} {
		t.Run(first, func(t *testing.T) {
			store, _, _ := openStore(t)
			createdAt := time.Date(2026, 11, 16, 9, 0, 0, 0, time.UTC)
			store.now = func() time.Time { return createdAt }
			id := uuid.New()
			name := "Test unrecorded Session"
			var row Thread
			var err error
			switch first {
			case "rename":
				row, err = store.Rename(t.Context(), id, &name)
			case "archive":
				row, err = store.Archive(t.Context(), id)
			case "unarchive":
				row, err = store.Unarchive(t.Context(), id)
			}
			if err != nil || row.ID != id || !row.CreatedAt.Equal(createdAt) || row.Archived != (first == "archive") {
				t.Fatalf("first write = %+v, %v", row, err)
			}
			store.now = func() time.Time { return createdAt.Add(time.Hour) }
			if _, err := store.Rename(t.Context(), id, &name); err != nil {
				t.Fatal(err)
			}
			row, err = store.Archive(t.Context(), id)
			if err != nil || row.Name == nil || *row.Name != name || !row.CreatedAt.Equal(createdAt) {
				t.Fatalf("archive overwrote metadata: %+v, %v", row, err)
			}
			row, err = store.Rename(t.Context(), id, nil)
			if err != nil || !row.Archived || row.Name != nil {
				t.Fatalf("rename overwrote archive: %+v, %v", row, err)
			}
			if err := store.OpenThread(t.Context(), id); err != nil {
				t.Fatal(err)
			}
			row, err = store.Unarchive(t.Context(), id)
			if err != nil || row.Archived || !row.CreatedAt.Equal(createdAt) {
				t.Fatalf("reopening overwrote metadata: %+v, %v", row, err)
			}
		})
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

func TestCommandNoticeAcknowledgementsRemainScopedToExactRevisions(t *testing.T) {
	store, _, _ := openStore(t)
	ctx := t.Context()
	session := NoticeScope{ThreadID: uuid.New(), Issuer: "https://operator.test/identity", Subject: "user/1"}
	otherSession := session
	otherSession.ThreadID = uuid.New()
	secondOperator := session
	secondOperator.Subject = "user/2"
	otherIssuer := session
	otherIssuer.Issuer = "https://other.test/identity"
	old := NoticeRevision{CommandID: "same-command", Version: 37}
	changed := NoticeRevision{CommandID: old.CommandID, Version: old.Version + 1}
	other := NoticeRevision{CommandID: "other-command", Version: 39}
	for _, notice := range []NoticeRevision{old, old, other} {
		if err := store.AcknowledgeNotice(ctx, session, notice); err != nil {
			t.Fatal(err)
		}
	}
	found, err := store.FindNoticeAcknowledgements(ctx, session, []NoticeRevision{old, changed, {CommandID: other.CommandID, Version: old.Version}})
	if err != nil || len(found) != 1 || found[0].NoticeRevision != old {
		t.Fatalf("exact revisions: %v, %v", found, err)
	}
	ackTime := found[0].AcknowledgedAt
	if rows, err := store.ListNoticeAcknowledgements(ctx, secondOperator, nil, 1); err != nil || len(rows) != 0 {
		t.Fatalf("another operator's history: %v, %v", rows, err)
	}
	if rows, err := store.FindNoticeAcknowledgements(ctx, secondOperator, []NoticeRevision{old}); err != nil || len(rows) != 0 {
		t.Fatalf("another operator's lookup: %v, %v", rows, err)
	}
	if err := store.AcknowledgeNotice(ctx, secondOperator, old); err != nil {
		t.Fatal(err)
	}
	if rows, err := store.FindNoticeAcknowledgements(ctx, secondOperator, []NoticeRevision{old}); err != nil || len(rows) != 1 {
		t.Fatalf("independent acknowledgement: %v, %v", rows, err)
	}
	found, err = store.FindNoticeAcknowledgements(ctx, otherSession, []NoticeRevision{old})
	if err != nil || len(found) != 0 {
		t.Fatalf("other Session: %v, %v", found, err)
	}

	if err := store.AcknowledgeNotice(ctx, session, changed); err != nil {
		t.Fatal(err)
	}
	if err := store.AcknowledgeNotice(ctx, session, old); err != nil {
		t.Fatal(err)
	}
	page, err := store.ListNoticeAcknowledgements(ctx, session, nil, 1)
	if err != nil || len(page) != 2 || page[0].NoticeRevision != changed || page[1].NoticeRevision != other {
		t.Fatalf("first page and lookahead: %v, %v", page, err)
	}
	for _, scope := range []NoticeScope{otherSession, secondOperator, otherIssuer} {
		if _, err := store.ListNoticeAcknowledgements(ctx, scope, &page[0].ID, 2); !errors.Is(err, ErrNoticeCursor) {
			t.Fatalf("cross-scope cursor: %v", err)
		}
	}
	if rows, err := store.FindNoticeAcknowledgements(ctx, otherIssuer, []NoticeRevision{old}); err != nil || len(rows) != 0 {
		t.Fatalf("different issuer with same subject: %v, %v", rows, err)
	}
	var issuer, subject string
	if err := store.db.QueryRowContext(ctx, "SELECT operator_issuer,operator_subject FROM command_notice_acknowledgement WHERE acknowledgement_id=$1", page[0].ID).Scan(&issuer, &subject); err != nil || issuer != session.Issuer || subject != session.Subject {
		t.Fatalf("persisted OIDC identity: issuer=%q subject=%q err=%v", issuer, subject, err)
	}
	next, err := store.ListNoticeAcknowledgements(ctx, session, &page[0].ID, 2)
	if err != nil || len(next) != 2 || next[0].NoticeRevision != other || next[1].NoticeRevision != old || !next[1].AcknowledgedAt.Equal(ackTime) {
		t.Fatalf("older revisions: %v, %v", next, err)
	}
}
