package store

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"strings"
	"testing"
	"time"

	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
)

// These tests use rows the retired Python service produced as semantic inputs, then write the typed columns the
// current store requires and hold the store to finding and reading them.

func (f fixture) insertInbox(t *testing.T, key string, ref DestinationRef, sessionID string) uuid.UUID {
	t.Helper()
	id := uuid.New()
	f.insertInboxAs(t, id, key, ref, sessionID)
	return id
}

func (f fixture) insertInboxAs(t *testing.T, id uuid.UUID, key string, ref DestinationRef, sessionID string) {
	t.Helper()
	f.exec(t, `
		INSERT INTO inbox (id, owner_namespace, owner_name, destination_key,
			destination_namespace, destination_name, destination_uid, session_id,
			last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, 0, 0, 0, false, now(), now(), now())`,
		id, f.owner.Namespace, f.owner.Name, key, ref.Namespace, ref.Name, ref.UID, sessionID)
}

// insertSubscription decodes a historical Python creation document and writes its current typed representation.
func (f fixture) insertSubscription(t *testing.T, inboxID uuid.UUID, creation json.RawMessage) uuid.UUID {
	t.Helper()
	var sent Subscribe
	if err := json.Unmarshal(creation, &sent); err != nil {
		t.Fatal(err)
	}
	creationProto, provider, err := encodeSubscriptionCreation(sent)
	if err != nil {
		t.Fatal(err)
	}
	id := uuid.New()
	expires := time.Now().Add(24 * time.Hour)
	switch source := sent.Source.(type) {
	case ActionsSource:
		f.exec(t, `
			INSERT INTO subscription (id, inbox_id, idempotency_key, creation_proto, source_provider, version,
				actions_after_sequence, generation, cancelled, expires_at, next_attempt)
			VALUES ($1, $2, $3, $4, $5, 1, $6, 0, false, $7, now())`,
			id, inboxID, sent.IdempotencyKey, creationProto, provider, source.AfterSequence, expires)
		return id
	case GitHubSource:
		subjectKind, subjectKey := githubSubjectStorage(t, source.Subject)
		f.exec(t, `INSERT INTO github_installation (app_id, installation_id, generation) VALUES (1, 2, 0) ON CONFLICT DO NOTHING`)
		var repositoryID int64
		if err := f.db.QueryRowContext(t.Context(),
			`INSERT INTO github_repository (full_name) VALUES ($1) RETURNING repository_id`, source.Repository).Scan(&repositoryID); err != nil {
			t.Fatal(err)
		}
		f.exec(t, `INSERT INTO github_repository_access (app_id, installation_id, repository_id, generation) VALUES (1, 2, $1, 0)`, repositoryID)
		f.exec(t, `INSERT INTO github_subject (repository_id, kind, subject_key, generation) VALUES ($1, $2, $3, 0)`, repositoryID, subjectKind, subjectKey)
		f.exec(t, `
			INSERT INTO subscription (id, inbox_id, idempotency_key, creation_proto, source_provider, version,
				github_start_position, generation, github_app_id, github_installation_id, github_repository_id,
				github_subject_kind, github_subject_key, cancelled, expires_at, next_attempt)
			VALUES ($1, $2, $3, $4, $5, 1, 0, 0, 1, 2, $6, $7, $8, false, $9, now())`,
			id, inboxID, sent.IdempotencyKey, creationProto, provider, repositoryID, subjectKind, subjectKey, expires)
		return id
	default:
		t.Fatalf("unsupported historical source %T", sent.Source)
		return uuid.Nil
	}
}

func githubSubjectStorage(t *testing.T, subject GitHubSubject) (string, string) {
	t.Helper()
	switch subject := subject.(type) {
	case PullRequestSubject:
		return "pull_request", strconv.FormatInt(subject.Number, 10)
	case IssueSubject:
		return "issue", strconv.FormatInt(subject.Number, 10)
	case BranchSubject:
		return "branch", subject.Name
	case CommitSubject:
		return "commit", subject.SHA
	default:
		t.Fatalf("unsupported historical GitHub subject %T", subject)
		return "", ""
	}
}

func TestInboxesPythonCreatedAreFoundByTheirDestinationKey(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	stored := 0
	incarnations := map[string]bool{}
	for _, vector := range loadGolden(t).DestinationKeys {
		// PostgreSQL cannot store a NUL, so no inbox row has a destination key made from one.
		if strings.ContainsRune(vector.SessionID+vector.DestinationRef.Namespace+vector.DestinationRef.Name+vector.DestinationRef.UID, 0) {
			continue
		}
		stored++
		incarnations[vector.DestinationRef.UID] = true
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
	listed := 0
	for uid := range incarnations {
		inboxes, err := f.ListInboxes(t.Context(), Owner{Account: f.owner, SandboxUID: uid})
		if err != nil {
			t.Fatal(err)
		}
		listed += len(inboxes)
	}
	if listed != stored {
		t.Fatalf("%d inboxes, want the %d Python created", listed, stored)
	}
}

func TestSubscriptionsPythonCreatedAreReadAndFoundByTheirKey(t *testing.T) {
	for _, vector := range loadGolden(t).Subscriptions {
		t.Run(vector.Name, func(t *testing.T) {
			f := newIsolatedFixture(t, defaultQuotas())
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
			// The shared state Python wrote for a GitHub subscription is what its status reports: the one grant it is
			// bound through, not yet validated, and the subject.
			var access *GitHubAccessKey
			if _, github := sent.Source.(GitHubSource); github {
				status := read.GitHub
				if status == nil || len(status.Access) != 1 || status.Access[0].CurrentlyValid ||
					status.Access[0].Key.AppID != 1 || status.Access[0].Key.InstallationID != 2 ||
					status.Subject.Key.RepositoryID != status.Access[0].Key.RepositoryID {
					t.Fatalf("GitHub status = %+v, want the one grant of the binding and its subject", status)
				}
				access = &status.Access[0].Key
			} else if read.GitHub != nil {
				t.Errorf("GitHub status = %+v, want none", read.GitHub)
			}
			listed, err := f.ListSubscriptions(t.Context(), Owner{Account: f.owner, SandboxUID: sent.DestinationRef.UID}, nil)
			if err != nil {
				t.Fatal(err)
			}
			if len(listed) != 1 || listed[0].ID != id {
				t.Errorf("listed %+v, want only the subscription Python created", listed)
			}
			// The same creation again is the retry of the one Python stored, for either source.
			retried, err := f.Subscribe(t.Context(), f.owner, sent, access)
			if err != nil || retried.ID != id {
				t.Errorf("a retry = %+v, %v; want the stored %s", retried, err, id)
			}
		})
	}
}

func TestEntriesPythonWroteAreReadAndNotAppendedAgain(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("first"))
	vectors := loadGolden(t).Events
	for i, vector := range vectors {
		event, err := DecodeEventIdentity(vector.Event)
		if err != nil {
			t.Fatal(err)
		}
		insertContentBackfilledEntry(t, f.db, subscription.InboxID, int64(i+1), vector.Event, entryPayloadForTest(t, event))
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

// pythonNotice is the notice Python prepared for an inbox that had acknowledged 2 entries and held 3.
const pythonNotice = "partly acknowledged"

func goldenNotice(t *testing.T, name string) (inboxID uuid.UUID, acknowledged, through int64, text string) {
	t.Helper()
	for _, vector := range loadGolden(t).Notices {
		if vector.Name == name {
			return vector.InboxID, vector.Acknowledged, vector.Through, vector.Text
		}
	}
	t.Fatalf("no notice vector %q", name)
	return
}

// goldenRunnerEntry is the History entry of the vector, and the bytes Python keeps of it in `notice.runner_entry`.
func goldenRunnerEntry(t *testing.T, name string) (*eventlogpb.EventEntry, []byte) {
	t.Helper()
	for _, vector := range loadGolden(t).RunnerEntries {
		if vector.Name == name {
			serialized, err := hex.DecodeString(vector.Serialized)
			if err != nil {
				t.Fatal(err)
			}
			return runnerEntry(t, vector.Entry), serialized
		}
	}
	t.Fatalf("no runner entry vector %q", name)
	return nil, nil
}

// insertPythonInbox writes the inbox the notice vector is for, with the cursors it had when Python prepared it.
func (f fixture) insertPythonInbox(t *testing.T) (inboxID uuid.UUID, acknowledged, through int64, text string) {
	t.Helper()
	inboxID, acknowledged, through, text = goldenNotice(t, pythonNotice)
	ref := body("first").DestinationRef
	f.insertInboxAs(t, inboxID, DestinationKey(ref, "test-session"), ref, "test-session")
	f.exec(t, `UPDATE inbox SET last_cursor = $2, acknowledged = $3 WHERE id = $1`, inboxID, through, acknowledged)
	return inboxID, acknowledged, through, text
}

// claimPythonNotice claims the inbox with the notice Python prepared and attempted, its command ID in the case
// the vectors parse it from, and the runner cursor and entry that Python had stored for it.
func (f fixture) claimPythonNotice(t *testing.T, runnerCursor int64, runnerEntry []byte) (Claim, Notice) {
	t.Helper()
	inboxID, _, through, text := f.insertPythonInbox(t)
	f.exec(t, `UPDATE inbox SET covered = $2 WHERE id = $1`, inboxID, through)
	commandID, err := uuid.Parse(loadGolden(t).CommandID.Input)
	if err != nil {
		t.Fatal(err)
	}
	// Python stores NULL until the first receipt, not an empty entry.
	var stored any
	if runnerEntry != nil {
		stored = runnerEntry
	}
	f.exec(t, `
		INSERT INTO notice (inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor, runner_entry)
		VALUES ($1, $2, $3, $4, true, false, false, $5, $6)`, inboxID, commandID, through, text, runnerCursor, stored)
	claim := f.claim(t)
	return claim, f.currentNotice(t, claim)
}

func TestNoticesGoPreparesHaveTheTextPythonPrepares(t *testing.T) {
	f := newClaimFixture(t)
	inboxID, _, through, text := f.insertPythonInbox(t)
	notice := f.prepareNotice(t, f.claim(t))
	requireEqual(t, notice.Text, text)
	requireEqual(t, notice.ThroughCursor, through)
	requireEqual(t, f.read(t, inboxID, 0).Inbox.Covered, through)
}

func TestRunnerReceiptsFollowANoticePythonPrepared(t *testing.T) {
	for _, c := range []struct {
		name  string
		entry string
		// want is how the notice differs once the receipt is copied. A nil want is a receipt the notice refuses.
		want func(*Notice)
	}{
		{"admission", "command admitted with the notice text", func(n *Notice) { n.Admitted = true }},
		{"confirmation among other commands", "confirmation naming several commands", func(n *Notice) { n.Confirmed = true }},
		{"failure", "command failed", func(n *Notice) { n.Error = ptr("failed") }},
		{"no-op", "command no-op", func(n *Notice) { n.Error = ptr("no-op") }},
		{"another command's admission", "command admitted for another command", func(*Notice) {}},
		{"admission of other content", "command admitted with other text", nil},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newClaimFixture(t)
			claim, notice := f.claimPythonNotice(t, 0, nil)
			entry, wire := goldenRunnerEntry(t, c.entry)
			cursor := int64(entry.GetCursor())
			if c.want == nil {
				requireInvalidHistory(t, f.Receipt(t.Context(), claim, notice, entry, 0))
				requireEqual(t, f.currentNotice(t, claim), notice)
				return
			}

			f.receipt(t, claim, notice, entry, 0)
			want := notice
			want.RunnerCursor = cursor
			c.want(&want)
			got := f.currentNotice(t, claim)
			if (got.Error != nil) != (want.Error != nil) {
				t.Errorf("notice error = %v, want one = %v", got.Error, want.Error != nil)
			}
			got.Error, want.Error = nil, nil
			requireEqual(t, got, want)
			if !bytes.Equal(f.storedRunnerEntry(t, claim.ID), wire) {
				t.Error("the boundary entry is not the bytes Python stores for it")
			}
			// Copying the same entry again, as a restarted worker does to verify the boundary, changes nothing.
			f.receipt(t, claim, notice, entry, cursor-1)
			requireEqual(t, f.currentNotice(t, claim).RunnerCursor, cursor)
		})
	}
}

func TestABoundaryPythonStoredIsVerifiedAgainstHistory(t *testing.T) {
	entry, wire := goldenRunnerEntry(t, "command admitted with the notice text")
	cursor := int64(entry.GetCursor())
	f := newClaimFixture(t)
	claim, notice := f.claimPythonNotice(t, cursor, wire)

	f.receipt(t, claim, notice, entry, cursor-1)
	other, _ := goldenRunnerEntry(t, "command admitted with other text")
	if other.GetCursor() != entry.GetCursor() {
		t.Fatalf("the vectors disagree on the cursor: %d and %d", other.GetCursor(), entry.GetCursor())
	}
	requireInvalidHistory(t, f.Receipt(t.Context(), claim, notice, other, cursor-1))
}
