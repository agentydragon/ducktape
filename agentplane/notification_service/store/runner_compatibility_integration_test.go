package store

import (
	"bytes"
	"encoding/hex"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
)

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

func (f fixture) insertPythonInbox(t *testing.T) (inboxID uuid.UUID, acknowledged, through int64, text string) {
	t.Helper()
	inboxID, acknowledged, through, text = goldenNotice(t, pythonNotice)
	ref := body("first").DestinationRef
	f.exec(t, `INSERT INTO inbox (id, owner_namespace, owner_name, destination_key,
		destination_namespace, destination_name, destination_uid, session_id,
		last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, $2, $3, $4, $5, $6, $7, 'test-session', $8, $9, 0, 0, false, now(), now(), now())`,
		inboxID, f.owner.Namespace, f.owner.Name, model.DestinationKey(ref, "test-session"),
		ref.Namespace, ref.Name, ref.UID, through, acknowledged)
	return inboxID, acknowledged, through, text
}

func (f fixture) claimPythonNotice(t *testing.T, runnerCursor int64, runnerEntry []byte) (Claim, Notice) {
	t.Helper()
	inboxID, _, through, text := f.insertPythonInbox(t)
	f.exec(t, `UPDATE inbox SET covered = $2 WHERE id = $1`, inboxID, through)
	commandID := uuid.MustParse(loadGolden(t).CommandID.Input)
	var stored any
	if runnerEntry != nil {
		stored = runnerEntry
	}
	f.exec(t, `INSERT INTO notice
		(inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor, runner_entry)
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
		want  func(*Notice)
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
