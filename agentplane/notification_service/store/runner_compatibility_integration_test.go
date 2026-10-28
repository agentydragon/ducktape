package store

import (
	"bytes"
	"encoding/hex"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
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

func TestPythonRunnerBoundaryAcceptsEquivalentWireOrdering(t *testing.T) {
	entry, pythonWire := goldenRunnerEntry(t, "command admitted with the notice text")
	cursor := int64(entry.GetCursor())
	stored := reverseWireFieldsAtPath(t, pythonWire, nil)
	incomingWire, err := marshalEntry(entry)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(incomingWire, stored) {
		t.Fatal("the stored alternate encoding has the same wire bytes as the incoming entry")
	}
	f := newClaimFixture(t)
	claim, notice := f.claimPythonNotice(t, cursor, stored)

	f.receipt(t, claim, notice, entry, cursor-1)
	if !bytes.Equal(f.storedRunnerEntry(t, claim.ID), stored) {
		t.Fatal("replaying an equivalent entry rewrote the stored alternate encoding")
	}
}

func TestRunnerBoundaryAcceptsEquivalentKnownWireForms(t *testing.T) {
	entry, _ := goldenRunnerEntry(t, "command admitted with the notice text")
	for _, c := range []struct {
		name     string
		sequence uint64
		wire     uint64
	}{
		{name: "explicit scalar default"},
		{name: "duplicate known scalar", sequence: 7, wire: 7},
	} {
		t.Run(c.name, func(t *testing.T) {
			storedEntry := &eventlogpb.EventEntry{
				Cursor: entry.GetCursor(),
				Origin: &eventlogpb.EventOrigin{SourceId: "runner", Sequence: c.sequence},
				Event:  entry.GetEvent(),
			}
			stored, err := marshalEntry(storedEntry)
			if err != nil {
				t.Fatal(err)
			}
			storedAlternate := appendVarintFieldAtPath(t, stored, []protowire.Number{2}, 2, c.wire)
			if bytes.Equal(storedAlternate, stored) {
				t.Fatal("equivalent wire form did not change the bytes")
			}

			f := newClaimFixture(t)
			claim, notice := f.claimPythonNotice(t, int64(entry.GetCursor()), storedAlternate)
			f.receipt(t, claim, notice, storedEntry, int64(entry.GetCursor())-1)
			if !bytes.Equal(f.storedRunnerEntry(t, claim.ID), storedAlternate) {
				t.Fatal("replaying an equivalent entry rewrote the stored boundary")
			}
		})
	}
}

func TestRunnerBoundaryAcceptsTheSameUnknownFieldsInDifferentNumberOrder(t *testing.T) {
	entry, base := goldenRunnerEntry(t, "command admitted with the notice text")
	stored := base
	for _, field := range []struct {
		path   []protowire.Number
		number protowire.Number
		value  uint64
	}{
		{nil, 100, 1},
		{nil, 101, 2},
		{[]protowire.Number{3}, 102, 3},
		{[]protowire.Number{3}, 103, 4},
		{[]protowire.Number{3, 8, 1}, 104, 5},
		{[]protowire.Number{3, 8, 1}, 105, 6},
	} {
		stored = appendVarintFieldAtPath(t, stored, field.path, field.number, field.value)
	}
	incoming := reverseWireFieldsAtPath(t, stored, nil)
	incoming = reverseWireFieldsAtPath(t, incoming, []protowire.Number{3})
	incoming = reverseWireFieldsAtPath(t, incoming, []protowire.Number{3, 8, 1})
	if bytes.Equal(incoming, stored) {
		t.Fatal("reordered unknown fields have the same wire bytes")
	}

	f := newClaimFixture(t)
	claim, notice := f.claimPythonNotice(t, int64(entry.GetCursor()), stored)
	f.receipt(t, claim, notice, decodeRunnerEntry(t, incoming), int64(entry.GetCursor())-1)
	if !bytes.Equal(f.storedRunnerEntry(t, claim.ID), stored) {
		t.Fatal("replaying equivalent unknown fields rewrote the stored boundary")
	}
}

func TestRunnerBoundaryRejectsChangedUnknownFieldsWithoutChangingState(t *testing.T) {
	entry, base := goldenRunnerEntry(t, "command admitted with the notice text")
	for _, c := range []struct {
		name   string
		stored []byte
		got    []byte
	}{
		{"added top-level field", base, appendVarintFieldAtPath(t, base, nil, 100, 1)},
		{"removed top-level field", appendVarintFieldAtPath(t, base, nil, 100, 1), base},
		{"changed top-level field", appendVarintFieldAtPath(t, base, nil, 100, 1), appendVarintFieldAtPath(t, base, nil, 100, 2)},
		{"added nested field", base, appendVarintFieldAtPath(t, base, []protowire.Number{3}, 100, 1)},
		{"removed nested field", appendVarintFieldAtPath(t, base, []protowire.Number{3}, 100, 1), base},
		{"changed nested field", appendVarintFieldAtPath(t, base, []protowire.Number{3}, 100, 1), appendVarintFieldAtPath(t, base, []protowire.Number{3}, 100, 2)},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newClaimFixture(t)
			claim, notice := f.claimPythonNotice(t, int64(entry.GetCursor()), c.stored)
			requireInvalidHistory(t, f.Receipt(t.Context(), claim, notice, decodeRunnerEntry(t, c.got), int64(entry.GetCursor())-1))
			requireEqual(t, f.currentNotice(t, claim), notice)
			if !bytes.Equal(f.storedRunnerEntry(t, claim.ID), c.stored) {
				t.Fatal("rejecting changed unknown fields changed the stored boundary")
			}
		})
	}
}

func TestRunnerBoundaryRejectsAddedOrChangedOriginWithoutChangingState(t *testing.T) {
	entry, pythonWire := goldenRunnerEntry(t, "command admitted with the notice text")
	withOrigin := &eventlogpb.EventEntry{
		Cursor: entry.GetCursor(),
		Origin: &eventlogpb.EventOrigin{SourceId: "runner", Sequence: 1},
		Event:  entry.GetEvent(),
	}
	withOriginWire, err := marshalEntry(withOrigin)
	if err != nil {
		t.Fatal(err)
	}
	changedOrigin := proto.Clone(withOrigin).(*eventlogpb.EventEntry)
	changedOrigin.Origin.SourceId = "another-runner"
	changedOriginWire, err := marshalEntry(changedOrigin)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		name   string
		stored []byte
		got    []byte
	}{
		{"new known origin field", pythonWire, withOriginWire},
		{"changed origin", withOriginWire, changedOriginWire},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newClaimFixture(t)
			claim, notice := f.claimPythonNotice(t, int64(entry.GetCursor()), c.stored)
			requireInvalidHistory(t, f.Receipt(t.Context(), claim, notice, decodeRunnerEntry(t, c.got), int64(entry.GetCursor())-1))
			requireEqual(t, f.currentNotice(t, claim), notice)
			if !bytes.Equal(f.storedRunnerEntry(t, claim.ID), c.stored) {
				t.Fatal("rejecting an origin difference changed the stored boundary")
			}
		})
	}
}

func TestRunnerBoundaryRejectsMalformedStoredProtobufWithoutChangingState(t *testing.T) {
	entry, _ := goldenRunnerEntry(t, "command admitted with the notice text")
	cursor := int64(entry.GetCursor())
	corrupt := []byte{0x80}
	f := newClaimFixture(t)
	claim, notice := f.claimPythonNotice(t, cursor, corrupt)

	requireInvalidHistory(t, f.Receipt(t.Context(), claim, notice, entry, cursor-1))
	requireEqual(t, f.currentNotice(t, claim), notice)
	if !bytes.Equal(f.storedRunnerEntry(t, claim.ID), corrupt) {
		t.Fatal("rejecting a malformed stored boundary changed its bytes")
	}
}

func decodeRunnerEntry(t *testing.T, wire []byte) *eventlogpb.EventEntry {
	t.Helper()
	var entry eventlogpb.EventEntry
	if err := proto.Unmarshal(wire, &entry); err != nil {
		t.Fatal(err)
	}
	return &entry
}

func appendVarintFieldAtPath(t *testing.T, wire []byte, path []protowire.Number, number protowire.Number, value uint64) []byte {
	t.Helper()
	return editWireMessageAtPath(t, wire, path, func(message []byte) []byte {
		message = protowire.AppendTag(message, number, protowire.VarintType)
		return protowire.AppendVarint(message, value)
	})
}

func reverseWireFieldsAtPath(t *testing.T, wire []byte, path []protowire.Number) []byte {
	t.Helper()
	return editWireMessageAtPath(t, wire, path, func(message []byte) []byte {
		fields := splitWireFields(t, message)
		for left, right := 0, len(fields)-1; left < right; left, right = left+1, right-1 {
			fields[left], fields[right] = fields[right], fields[left]
		}
		return joinWireFields(fields)
	})
}

func editWireMessageAtPath(t *testing.T, wire []byte, path []protowire.Number, edit func([]byte) []byte) []byte {
	t.Helper()
	if len(path) == 0 {
		return edit(wire)
	}
	fields := splitWireFields(t, wire)
	matches := 0
	for i, field := range fields {
		number, typ, tagSize := protowire.ConsumeTag(field)
		if number != path[0] {
			continue
		}
		if typ != protowire.BytesType {
			t.Fatalf("wire field %d has type %d, want length-delimited", number, typ)
		}
		nested, size := protowire.ConsumeBytes(field[tagSize:])
		if size < 0 || tagSize+size != len(field) {
			t.Fatalf("invalid length-delimited field %d: %s", number, protowire.ParseError(size))
		}
		nested = editWireMessageAtPath(t, nested, path[1:], edit)
		encoded := protowire.AppendTag(nil, number, typ)
		fields[i] = protowire.AppendBytes(encoded, nested)
		matches++
	}
	if matches != 1 {
		t.Fatalf("wire path %v matched %d fields, want one", path, matches)
	}
	return joinWireFields(fields)
}

func splitWireFields(t *testing.T, wire []byte) [][]byte {
	t.Helper()
	var fields [][]byte
	for len(wire) > 0 {
		number, typ, tagSize := protowire.ConsumeTag(wire)
		if tagSize < 0 {
			t.Fatalf("invalid protobuf tag: %s", protowire.ParseError(tagSize))
		}
		valueSize := protowire.ConsumeFieldValue(number, typ, wire[tagSize:])
		if valueSize < 0 {
			t.Fatalf("invalid protobuf field %d: %s", number, protowire.ParseError(valueSize))
		}
		fieldSize := tagSize + valueSize
		fields = append(fields, append([]byte(nil), wire[:fieldSize]...))
		wire = wire[fieldSize:]
	}
	return fields
}

func joinWireFields(fields [][]byte) []byte {
	var wire []byte
	for _, field := range fields {
		wire = append(wire, field...)
	}
	return wire
}
