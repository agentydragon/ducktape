package state

import (
	"crypto/sha256"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/publication"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
)

const (
	testFreshness      = 10 * time.Minute
	testRepositoryName = "test-owner/test-repo"
	forkName           = "test-fork-owner/test-repo"
)

var commitA = strings.Repeat("a", 40)

// Each fixture has a cloned PostgreSQL database and only GitHub source rows:
// no inbox, subscription, destination, or dependency on the inbox inbox.
type fixture struct {
	db                            *sql.DB
	state                         *State
	app, installation, repository int64
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	db := postgres.Open(t, integrationDatabase.Clone(t))
	g := fixture{db: db, state: New(db, transaction.New(db), testFreshness, publication.Adapter{}), app: 1, installation: 2, repository: 3}
	if err := g.state.Grant(t.Context(), g.access(), testRepositoryName); err != nil {
		t.Fatal(err)
	}
	return g
}

func (g fixture) access() AccessKey {
	return AccessKey{AppID: g.app, InstallationID: g.installation, RepositoryID: g.repository}
}

func (g fixture) subject(t *testing.T, number int64) SubjectKey {
	t.Helper()
	key := SubjectKey{RepositoryID: g.repository, Kind: "pull_request", Key: fmt.Sprint(number)}
	g.exec(t, `INSERT INTO github_subject (repository_id, kind, subject_key, generation)
		VALUES ($1, $2, $3, 0) ON CONFLICT DO NOTHING`, key.RepositoryID, key.Kind, key.Key)
	return key
}

func (g fixture) exec(t *testing.T, query string, args ...any) {
	t.Helper()
	if _, err := g.db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

func (g fixture) count(t *testing.T, query string, args ...any) int64 {
	t.Helper()
	var count int64
	if err := g.db.QueryRowContext(t.Context(), query, args...).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func (g fixture) status(t *testing.T, subject SubjectKey) Status {
	t.Helper()
	status, err := LoadStatus(t.Context(), g.db, g.app, g.installation, subject)
	if err != nil {
		t.Fatal(err)
	}
	return status
}

func (g fixture) validate(t *testing.T, key AccessKey) AccessFence {
	t.Helper()
	lease, err := g.state.Acquire(t.Context(), key)
	if err != nil {
		t.Fatal(err)
	}
	if lease != nil {
		if err := g.state.CompleteAccess(t.Context(), *lease, nil); err != nil {
			t.Fatal(err)
		}
	}
	fence, err := g.state.Fence(t.Context(), key)
	if err != nil {
		t.Fatal(err)
	}
	return fence
}

func (g fixture) refreshSubject(t *testing.T, subject SubjectKey, base AccessFence) {
	t.Helper()
	lease, err := g.state.Acquire(t.Context(), subject)
	if err != nil {
		t.Fatal(err)
	}
	if lease != nil {
		if err := g.state.CompleteSubject(t.Context(), *lease, []AccessFence{base}, SubjectObservation{}); err != nil {
			t.Fatal(err)
		}
	}
}

func (g fixture) addRevisions(t *testing.T, subject SubjectKey, revisions ...githubmodel.HeadRevision) {
	t.Helper()
	err := g.state.transaction(t.Context(), func(tx transaction.Tx) error {
		if err := Lock(t.Context(), tx); err != nil {
			return err
		}
		return AddRevisionsTx(t.Context(), tx, subject, revisions)
	})
	if err != nil {
		t.Fatal(err)
	}
}

func (g fixture) fork(offset int64) githubmodel.Repository {
	return githubmodel.Repository{ID: g.repository + offset, Name: forkName}
}

func (g fixture) grant(t *testing.T, installation int64, repository githubmodel.Repository) AccessKey {
	t.Helper()
	key := AccessKey{AppID: g.app, InstallationID: installation, RepositoryID: repository.ID}
	if err := g.state.Grant(t.Context(), key, repository.Name); err != nil {
		t.Fatal(err)
	}
	return key
}

func (g fixture) invalidateInstallation(t *testing.T) {
	t.Helper()
	payload := []byte(`{"action":"suspend"}`)
	digest := sha256.Sum256(append([]byte("installation\x00"), payload...))
	err := g.state.transaction(t.Context(), func(tx transaction.Tx) error {
		created, err := IngestTx(t.Context(), tx, Ingest{
			AppID: g.app, InstallationID: g.installation, DeliveryID: uuid.New(),
			Event: "installation", Digest: digest[:], Payload: payload,
		}, time.Now())
		if err == nil && !created {
			return errors.New("installation invalidation reused a delivery")
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}

func requireDeferred(t *testing.T, err error) *RefreshDeferredError {
	t.Helper()
	var deferred *RefreshDeferredError
	if !errors.As(err, &deferred) {
		t.Fatalf("error = %v, want *RefreshDeferredError", err)
	}
	return deferred
}

func requireDeferredAcquire(t *testing.T, g fixture, key RefreshKey) *RefreshDeferredError {
	t.Helper()
	lease, err := g.state.Acquire(t.Context(), key)
	if lease != nil {
		t.Fatalf("got lease %+v, want it deferred", lease)
	}
	return requireDeferred(t, err)
}
