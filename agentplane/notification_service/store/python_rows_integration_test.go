package store

import (
	"encoding/json"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

// These tests write rows the way the Python service does, from the golden vectors it produced, and hold the
// store to finding and reading them: the store takes over a database that Python filled.

func (f fixture) insertInbox(t *testing.T, key string, ref DestinationRef, sessionID string) uuid.UUID {
	t.Helper()
	destination, err := json.Marshal(ref)
	if err != nil {
		t.Fatal(err)
	}
	id := uuid.New()
	f.exec(t, `
		INSERT INTO inbox (id, owner_namespace, owner_name, destination_key, destination_ref, session_id,
			last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, $2, $3, $4, $5::jsonb, $6, 0, 0, 0, 0, false, now(), now(), now())`,
		id, f.owner.Namespace, f.owner.Name, key, string(destination), sessionID)
	return id
}

// insertSubscription writes a subscription of the inbox from the creation JSON Python stored, with the columns
// Python derives from it.
func (f fixture) insertSubscription(t *testing.T, inboxID uuid.UUID, creation json.RawMessage) uuid.UUID {
	t.Helper()
	var shape struct {
		IdempotencyKey string `json:"idempotency_key"`
		Source         struct {
			Provider      string `json:"provider"`
			AfterSequence int64  `json:"after_sequence"`
			Repository    string `json:"repository"`
			Subject       struct {
				Kind   string `json:"kind"`
				Number int64  `json:"number"`
				Name   string `json:"name"`
				SHA    string `json:"sha"`
			} `json:"subject"`
		} `json:"source"`
	}
	if err := json.Unmarshal(creation, &shape); err != nil {
		t.Fatal(err)
	}
	id := uuid.New()
	expires := time.Now().Add(24 * time.Hour)
	if shape.Source.Provider == "actions" {
		f.exec(t, `
			INSERT INTO subscription (id, inbox_id, idempotency_key, creation, version, actions_after_sequence, generation,
				cancelled, expires_at, next_attempt)
			VALUES ($1, $2, $3, $4::jsonb, 1, $5, 0, false, $6, now())`,
			id, inboxID, shape.IdempotencyKey, string(creation), shape.Source.AfterSequence, expires)
		return id
	}
	subject := shape.Source.Subject
	key := map[string]string{
		"pull_request": strconv.FormatInt(subject.Number, 10), "issue": strconv.FormatInt(subject.Number, 10),
		"branch": subject.Name, "commit": subject.SHA,
	}[subject.Kind]
	f.exec(t, `INSERT INTO github_installation (app_id, installation_id, generation) VALUES (1, 2, 0) ON CONFLICT DO NOTHING`)
	var repositoryID int64
	if err := f.db.QueryRowContext(t.Context(),
		`INSERT INTO github_repository (full_name) VALUES ($1) RETURNING repository_id`, shape.Source.Repository).Scan(&repositoryID); err != nil {
		t.Fatal(err)
	}
	f.exec(t, `INSERT INTO github_repository_access (app_id, installation_id, repository_id, generation) VALUES (1, 2, $1, 0)`, repositoryID)
	f.exec(t, `INSERT INTO github_subject (repository_id, kind, subject_key, generation) VALUES ($1, $2, $3, 0)`, repositoryID, subject.Kind, key)
	f.exec(t, `
		INSERT INTO subscription (id, inbox_id, idempotency_key, creation, version, github_start_position, generation,
			github_app_id, github_installation_id, github_repository_id, github_subject_kind, github_subject_key,
			cancelled, expires_at, next_attempt)
		VALUES ($1, $2, $3, $4::jsonb, 1, 0, 0, 1, 2, $5, $6, $7, false, $8, now())`,
		id, inboxID, shape.IdempotencyKey, string(creation), repositoryID, subject.Kind, key, expires)
	return id
}

func TestInboxesPythonCreatedAreFoundByTheirDestinationKey(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	stored := 0
	for _, vector := range loadGolden(t).DestinationKeys {
		// PostgreSQL cannot store a NUL, so no inbox row has a destination key made from one.
		if strings.ContainsRune(vector.SessionID+vector.DestinationRef.Namespace+vector.DestinationRef.Name+vector.DestinationRef.UID, 0) {
			continue
		}
		stored++
		t.Run(vector.Name, func(t *testing.T) {
			inboxID := f.insertInbox(t, vector.Key, vector.DestinationRef, vector.SessionID)
			creation := body("first")
			creation.DestinationRef = vector.DestinationRef
			creation.SessionID = vector.SessionID
			if got := f.subscribe(t, creation).InboxID; got != inboxID {
				t.Errorf("subscribed in inbox %s, not the one Python created, %s", got, inboxID)
			}
		})
	}
	inboxes, err := f.ListInboxes(t.Context(), f.owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(inboxes) != stored {
		t.Fatalf("%d inboxes, want the %d Python created", len(inboxes), stored)
	}
}

func TestSubscriptionsPythonCreatedAreReadAndFoundByTheirKey(t *testing.T) {
	for _, vector := range loadGolden(t).Subscriptions {
		t.Run(vector.Name, func(t *testing.T) {
			f := newFixture(t, defaultQuotas())
			var sent Subscribe
			if err := decode(vector.Input, &sent); err != nil {
				t.Fatal(err)
			}
			inboxID := f.insertInbox(t, DestinationKey(sent.DestinationRef, sent.SessionID), sent.DestinationRef, sent.SessionID)
			id := f.insertSubscription(t, inboxID, vector.Stored)

			read := f.get(t, id)
			if read.InboxID != inboxID || read.IdempotencyKey != sent.IdempotencyKey || read.Version != 1 || read.Cancelled ||
				!sourcesEqual(read.Source, sent.Source) {
				t.Errorf("read %+v, want a subscription following %+v", read, sent.Source)
			}
			listed, err := f.ListSubscriptions(t.Context(), f.owner, nil)
			if err != nil {
				t.Fatal(err)
			}
			if len(listed) != 1 || listed[0].ID != id {
				t.Errorf("listed %+v, want only the subscription Python created", listed)
			}
			// The same creation again is the retry of the one Python stored, for either source.
			if retried := f.subscribe(t, sent); retried.ID != id {
				t.Errorf("a retry created %s, not the stored %s", retried.ID, id)
			}
		})
	}
}

func TestEntriesPythonWroteAreReadAndNotAppendedAgain(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("first"))
	vectors := loadGolden(t).Events
	for i, vector := range vectors {
		f.exec(t, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at) VALUES ($1, $2, $3::jsonb, '{}'::jsonb, now())`,
			subscription.InboxID, i+1, string(vector.Event))
	}
	f.exec(t, `UPDATE inbox SET last_cursor = $2 WHERE id = $1`, subscription.InboxID, len(vectors))

	page := f.read(t, subscription.InboxID, 0)
	if len(page.Entries) != len(vectors) {
		t.Fatalf("%d entries, want %d", len(page.Entries), len(vectors))
	}
	for i, vector := range vectors {
		event, err := DecodeEventIdentity(vector.Event)
		if err != nil {
			t.Fatal(err)
		}
		requireEqual(t, page.Entries[i].Event, event)
		// The event Go writes is the identity of the entry Python wrote: no second entry is appended.
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, `{}`)
	}
	if got := f.read(t, subscription.InboxID, 0).Inbox.LastCursor; got != int64(len(vectors)) {
		t.Fatalf("last cursor = %d, want %d", got, len(vectors))
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, subscription.InboxID); got != len(vectors) {
		t.Fatalf("%d entries, want %d", got, len(vectors))
	}
}
