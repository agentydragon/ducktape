package fanout

import (
	"context"
	"database/sql"
	"os"
	"reflect"
	"testing"

	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var fanoutIntegrationDatabase *postgres.Database

type (
	Claim        = inboxstore.Claim
	Entry        = inboxstore.Entry
	Inbox        = inboxstore.Inbox
	InboxPage    = inboxstore.InboxPage
	Subscription = inboxstore.Subscription
)

var (
	ErrClaimLost            = inboxstore.ErrClaimLost
	ErrIdempotencyKeyReused = inboxstore.ErrIdempotencyKeyReused
)

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTestsWithCloneTemplate(m, migrations.Up, &fanoutIntegrationDatabase))
}

func defaultQuotas() inboxstore.Quotas {
	return inboxstore.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000}
}

// fixture is a public Store over the integration database. Each test uses a unique owner; tests that need an
// otherwise empty database use newIsolatedFixture.
type fixture struct {
	*inboxstore.Store
	db          *sql.DB
	databaseURL string
	runner      *transaction.Runner
	owner       model.ServiceAccount
	identity    model.Identity
	sandboxUID  string
	sessionID   string
}

func newFixture(t *testing.T, quotas inboxstore.Quotas) fixture {
	t.Helper()
	databaseURL := fanoutIntegrationDatabase.Clone(t)
	db := postgres.Open(t, databaseURL)
	return fixtureForDatabase(t, db, databaseURL, quotas)
}

func newIsolatedFixture(t *testing.T, quotas inboxstore.Quotas) fixture {
	t.Helper()
	return newFixture(t, quotas)
}

func newSharedFixture(t *testing.T, quotas inboxstore.Quotas) fixture {
	t.Helper()
	db := postgres.Open(t, fanoutIntegrationDatabase.URL)
	return fixtureForDatabase(t, db, fanoutIntegrationDatabase.URL, quotas)
}

func fixtureForDatabase(t *testing.T, db *sql.DB, databaseURL string, quotas inboxstore.Quotas) fixture {
	t.Helper()
	runner := transaction.New(db)
	return fixture{
		Store: inboxstore.New(db, runner, quotas), db: db, databaseURL: databaseURL, runner: runner, owner: newOwner(),
		identity:   model.Identity{Name: "identity-" + uuid.NewString(), UID: uuid.NewString()},
		sandboxUID: testSandboxUID, sessionID: uuid.NewString(),
	}
}

func newOwner() model.ServiceAccount {
	return model.ServiceAccount{Namespace: "test-namespace", Name: "owner-" + uuid.NewString()}
}

func (f fixture) exec(t *testing.T, query string, args ...any) {
	t.Helper()
	if _, err := f.db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

func (f fixture) count(t *testing.T, query string, args ...any) int {
	t.Helper()
	return postgres.Count(t, f.db, query, args...)
}

func (f fixture) transaction(ctx context.Context, run func(transaction.Tx) error) error {
	return f.runner.Run(ctx, run)
}

func (f fixture) insertInbox(t *testing.T, sessionID string) uuid.UUID {
	t.Helper()
	id := uuid.New()
	f.exec(t, `
		INSERT INTO inbox (id, session_id,
			last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until,
			owner_identity_name, owner_identity_uid)
		VALUES ($1, $2, 0, 0, 0, 0, false, now(), now(), now(), $3, $4)`,
		id, sessionID, f.identity.Name, f.identity.UID)
	return id
}

const testSandboxUID = "test-uid"

func (f fixture) incarnation() inboxstore.Owner {
	return inboxstore.Owner{Identity: f.identity, SandboxUID: f.sandboxUID}
}

func (f fixture) body(key string) sourceadapter.Subscribe {
	body := sourceadapter.Subscribe{
		SessionID:      f.sessionID,
		IdempotencyKey: key,
		Source:         sourceadapter.WrapActionsSource(model.ActionsSource{RequestID: uuid.New()}),
		LifetimeDays:   7,
	}
	return body
}

func (f fixture) subscribe(t *testing.T, creation sourceadapter.Subscribe) inboxstore.Subscription {
	t.Helper()
	subscription, err := f.Store.Subscribe(t.Context(), f.incarnation(), creation, sourceadapter.ActionsAuthorization{})
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) renew(t *testing.T, id uuid.UUID, version, lifetimeDays int64) inboxstore.Subscription {
	t.Helper()
	subscription, err := f.Renew(t.Context(), f.incarnation(), id, version, lifetimeDays)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) cancel(t *testing.T, id uuid.UUID) inboxstore.Subscription {
	t.Helper()
	subscription, err := f.Cancel(t.Context(), f.incarnation(), id)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) get(t *testing.T, id uuid.UUID) inboxstore.Subscription {
	t.Helper()
	subscription, err := f.GetSubscription(t.Context(), f.incarnation(), id)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) read(t *testing.T, inboxID uuid.UUID, after int64) inboxstore.InboxPage {
	t.Helper()
	page, err := f.Read(t.Context(), f.incarnation(), inboxID, after, 128)
	if err != nil {
		t.Fatal(err)
	}
	return page
}

func (f fixture) acknowledge(t *testing.T, inboxID uuid.UUID, through int64) inboxstore.Inbox {
	t.Helper()
	inbox, err := f.Acknowledge(t.Context(), f.incarnation(), inboxID, through)
	if err != nil {
		t.Fatal(err)
	}
	return inbox
}

func (f fixture) appendEvent(ctx context.Context, inboxID, subscriptionID uuid.UUID, event sourceadapter.EventIdentity, payload []byte) error {
	return f.transaction(ctx, func(tx transaction.Tx) error {
		if _, err := tx.ExecContext(ctx, `SELECT 1 FROM inbox WHERE id = $1 FOR UPDATE`, inboxID); err != nil {
			return err
		}
		input, err := sourceadapter.AppendInputFor(event, payload)
		if err != nil {
			return err
		}
		return f.AppendTx(ctx, tx, inboxID, subscriptionID, input)
	})
}

func requireEqual(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got  %+v\nwant %+v", got, want)
	}
}

func ptr[T any](value T) *T { return &value }
