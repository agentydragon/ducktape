package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		migrations.Up,
		func(database *postgres.Database) int {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
			defer cancel()
			if err := database.PrepareCloneTemplate(ctx); err != nil {
				fmt.Fprintln(os.Stderr, "prepare pristine PostgreSQL clone template:", err)
				return 1
			}
			integrationDatabase = database
			return m.Run()
		},
	))
}

func defaultQuotas() Quotas {
	return Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000}
}

// fixture is a Store over the shared database, for an owner no other test uses, so tests do not see each
// other's inboxes. The owner acts as the Sandbox incarnation sandboxUID, at first the one body's destination names.
type fixture struct {
	*Store
	db         *sql.DB
	owner      ServiceAccount
	sandboxUID string
}

func newFixture(t *testing.T, quotas Quotas) fixture {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return fixtureForDatabase(t, db, quotas)
}

func newIsolatedFixture(t *testing.T, quotas Quotas) fixture {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return fixtureForDatabase(t, db, quotas)
}

func fixtureForDatabase(t *testing.T, db *sql.DB, quotas Quotas) fixture {
	t.Helper()
	return fixture{Store: New(db, quotas), db: db, owner: newOwner(), sandboxUID: testSandboxUID}
}

func newOwner() ServiceAccount {
	return ServiceAccount{Namespace: "test-namespace", Name: "owner-" + uuid.NewString()}
}

// insertBackfilledEntry stores the native event JSON a prior writer created alongside the relational identity
// that the identity backfill has populated before the relational-reader release.
func insertBackfilledEntry(t *testing.T, db *sql.DB, inboxID uuid.UUID, cursor int64, eventJSON []byte, payload string) {
	t.Helper()
	event, err := DecodeEventIdentity(eventJSON)
	if err != nil {
		t.Fatal(err)
	}
	columns, err := eventIdentityStorageFor(event)
	if err != nil {
		t.Fatal(err)
	}
	args := append([]any{inboxID, cursor, string(eventJSON), payload}, columns.args()...)
	_, err = db.ExecContext(t.Context(), `INSERT INTO entry (inbox_id, cursor, event, payload, created_at,
		identity_provider, actions_request_id, actions_sequence,
		github_app_id, github_delivery_id, github_repository_id, github_event, github_action,
		github_pull_request_number, github_mergeability_sequence, github_snapshot_version,
		message_id, message_sender_namespace, message_sender_name)
		VALUES ($1, $2, $3::jsonb, $4::jsonb, now(), $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)`, args...)
	if err != nil {
		t.Fatal(err)
	}
}

// testSandboxUID is the Sandbox incarnation body's destination names.
const testSandboxUID = "test-uid"

// incarnation is the fixture's owner as its Sandbox incarnation.
func (f fixture) incarnation() Owner {
	return Owner{Account: f.owner, SandboxUID: f.sandboxUID}
}

// body is a creation following one Action request, for the fixture's one Session. Copies keep the request and
// change what they set.
func body(key string) Subscribe {
	return Subscribe{
		DestinationRef: DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: testSandboxUID},
		SessionID:      "test-session",
		IdempotencyKey: key,
		Source:         ActionsSource{RequestID: uuid.New()},
		LifetimeDays:   7,
	}
}

func (f fixture) subscribe(t *testing.T, creation Subscribe) Subscription {
	t.Helper()
	subscription, err := f.Subscribe(t.Context(), f.owner, creation, nil)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) renew(t *testing.T, id uuid.UUID, version, lifetimeDays int64) Subscription {
	t.Helper()
	subscription, err := f.Renew(t.Context(), f.incarnation(), id, version, lifetimeDays)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) cancel(t *testing.T, id uuid.UUID) Subscription {
	t.Helper()
	subscription, err := f.Cancel(t.Context(), f.incarnation(), id)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) get(t *testing.T, id uuid.UUID) Subscription {
	t.Helper()
	subscription, err := f.GetSubscription(t.Context(), f.incarnation(), id)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) read(t *testing.T, inboxID uuid.UUID, after int64) InboxPage {
	t.Helper()
	page, err := f.Read(t.Context(), f.incarnation(), inboxID, after, 128)
	if err != nil {
		t.Fatal(err)
	}
	return page
}

func (f fixture) acknowledge(t *testing.T, inboxID uuid.UUID, through int64) Inbox {
	t.Helper()
	inbox, err := f.Acknowledge(t.Context(), f.incarnation(), inboxID, through)
	if err != nil {
		t.Fatal(err)
	}
	return inbox
}

// appendEvent commits one event of a subscription to its inbox, as a source worker does: with the inbox locked.
func (f fixture) appendEvent(inboxID, subscriptionID uuid.UUID, event EventIdentity, payload string) error {
	ctx := context.Background()
	return f.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `SELECT 1 FROM inbox WHERE id = $1 FOR UPDATE`, inboxID); err != nil {
			return err
		}
		return f.AppendEvent(ctx, tx, inboxID, subscriptionID, event, json.RawMessage(payload))
	})
}

func (f fixture) mustAppendEvent(t *testing.T, inboxID, subscriptionID uuid.UUID, event EventIdentity, payload string) {
	t.Helper()
	if err := f.appendEvent(inboxID, subscriptionID, event, payload); err != nil {
		t.Fatal(err)
	}
}

func (f fixture) exec(t *testing.T, query string, args ...any) {
	t.Helper()
	if _, err := f.db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

func (f fixture) count(t *testing.T, query string, args ...any) int {
	t.Helper()
	var count int
	if err := f.db.QueryRowContext(t.Context(), query, args...).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

// cleanAll runs Cleanup over every inbox of the shared database, a page at a time.
func (f fixture) cleanAll(t *testing.T) {
	t.Helper()
	var after *uuid.UUID
	for {
		last, err := f.Cleanup(t.Context(), after)
		if err != nil {
			t.Fatal(err)
		}
		if last == nil {
			return
		}
		after = last
	}
}
