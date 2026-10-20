package store

import (
	"bytes"
	"errors"
	"strings"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
)

func admittedEntry(cursor uint64, commandID, text string) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{
		CommandAdmitted: &eventpb.CommandAdmitted{Command: &commandpb.Command{
			CommandId: commandID, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: text}},
		}},
	}}}
}

func confirmedEntry(cursor uint64, commandIDs ...string) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{
		HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{OriginCommandIds: commandIDs},
	}}}
}

func failedEntry(cursor uint64, commandID string) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: &eventpb.Event{Observation: &eventpb.Event_CommandFailed{
		CommandFailed: &eventpb.CommandFailed{CommandId: commandID, Reason: "refused"},
	}}}
}

func noopEntry(cursor uint64, commandID string) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: &eventpb.Event{Observation: &eventpb.Event_CommandNoop{
		CommandNoop: &eventpb.CommandNoop{CommandId: commandID, Reason: "nothing to do"},
	}}}
}

func unrelatedEntry(cursor uint64) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: &eventpb.Event{Observation: &eventpb.Event_HarnessLost{
		HarnessLost: &eventpb.HarnessLost{},
	}}}
}

func requireInvalidHistory(t *testing.T, err error) {
	t.Helper()
	var invalid *InvalidHistoryError
	if !errors.As(err, &invalid) {
		t.Fatalf("error = %v, want *InvalidHistoryError", err)
	}
	if !strings.HasPrefix(err.Error(), InvalidHistoryPrefix) {
		t.Errorf("error %q does not start with InvalidHistoryPrefix", err)
	}
}

func (f fixture) currentNotice(t *testing.T, claim Claim) Notice {
	t.Helper()
	notice, err := f.CurrentNotice(t.Context(), claim)
	if err != nil {
		t.Fatal(err)
	}
	if notice == nil {
		t.Fatal("the inbox has no notice")
	}
	return *notice
}

func (f fixture) storedRunnerEntry(t *testing.T, inboxID uuid.UUID) []byte {
	t.Helper()
	var stored []byte
	if err := f.db.QueryRowContext(t.Context(), `SELECT runner_entry FROM notice WHERE inbox_id = $1`, inboxID).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	return stored
}

func (f fixture) receipt(t *testing.T, claim Claim, notice Notice, entry *eventlogpb.EventEntry, afterCursor int64) {
	t.Helper()
	if err := f.Receipt(t.Context(), claim, notice, entry, afterCursor); err != nil {
		t.Fatal(err)
	}
}

// noticeFixture is a claimed inbox with a prepared notice for `events` entries.
type noticeFixture struct {
	fixture
	subscription Subscription
	held         Claim
	notice       Notice
}

func newNoticeFixture(t *testing.T, events int64) noticeFixture {
	t.Helper()
	f := newClaimFixture(t)
	subscription := f.subscribe(t, body("first"))
	f.addEvents(t, subscription, 1, events)
	claim := f.claim(t)
	return noticeFixture{fixture: f, subscription: subscription, held: claim, notice: f.prepareNotice(t, claim)}
}

func TestPrepareNoticeNamesEveryEntryOnceAndRepeatsAsTheSameCommand(t *testing.T) {
	f := newClaimFixture(t)
	subscription := f.subscribe(t, body("first"))
	claim := f.claim(t)
	if notice, err := f.PrepareNotice(t.Context(), claim); err != nil || notice != nil {
		t.Fatalf("PrepareNotice of an empty inbox = %+v, %v", notice, err)
	}

	f.addEvents(t, subscription, 1, 3)
	f.acknowledge(t, subscription.InboxID, 1)
	notice := f.prepareNotice(t, claim)
	if notice.CommandID == uuid.Nil {
		t.Error("the notice has no command ID")
	}
	requireEqual(t, notice, Notice{CommandID: notice.CommandID, ThroughCursor: 3, Text: noticeText(subscription.InboxID, 1, 3)})
	requireEqual(t, f.read(t, subscription.InboxID, 0).Inbox.Covered, int64(3))

	// A notice in flight is returned as it is, however many entries have come since.
	f.addEvents(t, subscription, 4, 4)
	requireEqual(t, f.prepareNotice(t, claim), notice)
	requireEqual(t, f.read(t, subscription.InboxID, 0).Inbox.Covered, int64(3))
}

// end ends the notice the way the runner or the sender does: "failed" with a terminal outcome of the sender's,
// "confirmed" with the harness's confirmation.
func (f noticeFixture) end(t *testing.T, how string) {
	t.Helper()
	switch how {
	case "failed":
		if err := f.DeliveryFailed(t.Context(), f.held, f.notice, "failed"); err != nil {
			t.Fatal(err)
		}
	case "confirmed":
		f.receipt(t, f.held, f.notice, confirmedEntry(5, f.notice.CommandID.String()), 0)
	default:
		t.Fatalf("unknown ending %q", how)
	}
}

// settle makes the inbox's entries needless to tell the agent of: it acknowledges them, or their content
// expires.
func (f fixture) settle(t *testing.T, inboxID uuid.UUID, how string, through int64) {
	t.Helper()
	switch how {
	case "acknowledged":
		f.acknowledge(t, inboxID, through)
	case "expired":
		f.exec(t, `UPDATE inbox SET expired_through = $2 WHERE id = $1`, inboxID, through)
	default:
		t.Fatalf("unknown way to settle %q", how)
	}
}

func TestANoticeThatEndedIsFollowedByAnotherOnlyForNewEntries(t *testing.T) {
	for _, how := range []string{"failed", "confirmed"} {
		t.Run(how, func(t *testing.T) {
			f := newNoticeFixture(t, 2)
			f.end(t, how)
			if notice, err := f.PrepareNotice(t.Context(), f.held); err != nil || notice != nil {
				t.Fatalf("PrepareNotice with nothing new = %+v, %v", notice, err)
			}

			f.addEvents(t, f.subscription, 3, 3)
			second := f.prepareNotice(t, f.held)
			if second.CommandID == f.notice.CommandID {
				t.Error("the new entries were given to the old notice")
			}
			requireEqual(t, second, Notice{CommandID: second.CommandID, ThroughCursor: 3, Text: noticeText(f.subscription.InboxID, 0, 3)})
			// Only the latest notice is kept.
			requireEqual(t, f.count(t, `SELECT count(*) FROM notice WHERE inbox_id = $1`, f.subscription.InboxID), 1)
			requireEqual(t, f.read(t, f.subscription.InboxID, 0).Notice, &second)
		})
	}
}

func TestNoNoticeIsPreparedForEntriesNobodyNeedsToBeTold(t *testing.T) {
	for _, how := range []string{"acknowledged", "expired"} {
		t.Run(how, func(t *testing.T) {
			f := newClaimFixture(t)
			subscription := f.subscribe(t, body("first"))
			f.addEvents(t, subscription, 1, 2)
			claim := f.claim(t)
			f.settle(t, subscription.InboxID, how, 2)
			if notice, err := f.PrepareNotice(t.Context(), claim); err != nil || notice != nil {
				t.Fatalf("PrepareNotice = %+v, %v", notice, err)
			}
			requireEqual(t, f.count(t, `SELECT count(*) FROM notice WHERE inbox_id = $1`, subscription.InboxID), 0)
		})
	}
}

func TestAttemptMarksTheNoticeAsAboutToBeSubmitted(t *testing.T) {
	f := newNoticeFixture(t, 2)
	attempt, err := f.Attempt(t.Context(), f.held, f.notice)
	if err != nil || !attempt {
		t.Fatalf("Attempt = %v, %v", attempt, err)
	}
	attempted := f.currentNotice(t, f.held)
	requireEqual(t, attempted, Notice{CommandID: f.notice.CommandID, ThroughCursor: 2, Text: f.notice.Text, Attempted: true})

	// Once attempted, the notice may have reached the runner, so the retry is not suppressed by what the agent
	// has done since.
	f.acknowledge(t, f.subscription.InboxID, 2)
	attempt, err = f.Attempt(t.Context(), f.held, attempted)
	if err != nil || !attempt {
		t.Fatalf("a retry after acknowledgement = %v, %v", attempt, err)
	}
	requireEqual(t, f.currentNotice(t, f.held), attempted)
}

func TestAttemptIsSuppressedWhenTheEntriesAreSettledBeforeIt(t *testing.T) {
	for _, how := range []string{"acknowledged", "expired"} {
		t.Run(how, func(t *testing.T) {
			f := newNoticeFixture(t, 2)
			f.settle(t, f.subscription.InboxID, how, 2)
			attempt, err := f.Attempt(t.Context(), f.held, f.notice)
			if err != nil || attempt {
				t.Fatalf("Attempt = %v, %v; want it suppressed", attempt, err)
			}
			ended := f.currentNotice(t, f.held)
			if ended.Attempted || ended.Error == nil {
				t.Errorf("notice = %+v, want an ended notice that was never attempted", ended)
			}
			// It covered the entries, so no other notice is prepared for them.
			if notice, err := f.PrepareNotice(t.Context(), f.held); err != nil || notice != nil {
				t.Errorf("PrepareNotice = %+v, %v", notice, err)
			}
		})
	}
}

func TestReceiptsCopyWhatTheRunnerRecordedForTheNotice(t *testing.T) {
	for _, c := range []struct {
		name  string
		entry func(commandID, text string) *eventlogpb.EventEntry
		want  func(notice *Notice)
	}{
		{
			"admission", func(id, text string) *eventlogpb.EventEntry { return admittedEntry(5, id, text) },
			func(notice *Notice) { notice.Admitted = true },
		},
		{
			"confirmation among other commands", func(id, _ string) *eventlogpb.EventEntry { return confirmedEntry(5, "coalesced", id) },
			func(notice *Notice) { notice.Confirmed = true },
		},
		{
			"failure", func(id, _ string) *eventlogpb.EventEntry { return failedEntry(5, id) },
			func(notice *Notice) { notice.Error = ptr("failed") },
		},
		{
			"no-op", func(id, _ string) *eventlogpb.EventEntry { return noopEntry(5, id) },
			func(notice *Notice) { notice.Error = ptr("no-op") },
		},
		{
			"another command's admission", func(_, _ string) *eventlogpb.EventEntry { return admittedEntry(5, "another", "text") },
			func(*Notice) {},
		},
		{
			"another command's failure", func(_, _ string) *eventlogpb.EventEntry { return failedEntry(5, "another") },
			func(*Notice) {},
		},
		{
			"unrelated event", func(_, _ string) *eventlogpb.EventEntry { return unrelatedEntry(5) },
			func(*Notice) {},
		},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newNoticeFixture(t, 2)
			if attempt, err := f.Attempt(t.Context(), f.held, f.notice); err != nil || !attempt {
				t.Fatalf("Attempt = %v, %v", attempt, err)
			}
			f.receipt(t, f.held, f.notice, c.entry(f.notice.CommandID.String(), f.notice.Text), 0)

			want := Notice{CommandID: f.notice.CommandID, ThroughCursor: 2, Text: f.notice.Text, Attempted: true, RunnerCursor: 5}
			c.want(&want)
			got := f.currentNotice(t, f.held)
			// An ended notice shows why, in words the receipt chooses; that it ended is what matters.
			if (got.Error != nil) != (want.Error != nil) {
				t.Errorf("notice error = %v, want one = %v", got.Error, want.Error != nil)
			}
			got.Error, want.Error = nil, nil
			requireEqual(t, got, want)
		})
	}
}

func TestReceiptsRefuseHistoryThatContradictsTheNotice(t *testing.T) {
	for _, c := range []struct {
		name  string
		entry func(commandID string) *eventlogpb.EventEntry
		after int64
	}{
		{"an admission of different content", func(id string) *eventlogpb.EventEntry { return admittedEntry(6, id, "different") }, 5},
		{"a gap after the committed cursor", func(string) *eventlogpb.EventEntry { return unrelatedEntry(9) }, 6},
		{"an entry before the committed cursor", func(string) *eventlogpb.EventEntry { return unrelatedEntry(4) }, 0},
		{"a changed entry at the committed cursor", func(id string) *eventlogpb.EventEntry { return failedEntry(5, "another") }, 4},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newNoticeFixture(t, 2)
			f.receipt(t, f.held, f.notice, unrelatedEntry(5), 0)
			committed := f.currentNotice(t, f.held)
			storedBefore := f.storedRunnerEntry(t, f.subscription.InboxID)

			requireInvalidHistory(t, f.Receipt(t.Context(), f.held, f.notice, c.entry(f.notice.CommandID.String()), c.after))
			requireEqual(t, f.currentNotice(t, f.held), committed)
			if !bytes.Equal(f.storedRunnerEntry(t, f.subscription.InboxID), storedBefore) {
				t.Error("the committed boundary entry changed")
			}
		})
	}
}

func TestReceiptsAcceptReplaysAndRereadsOfCommittedHistory(t *testing.T) {
	f := newNoticeFixture(t, 2)
	f.receipt(t, f.held, f.notice, unrelatedEntry(5), 0)
	boundary := f.storedRunnerEntry(t, f.subscription.InboxID)

	// The boundary read again after a restart.
	f.receipt(t, f.held, f.notice, unrelatedEntry(5), 4)
	requireEqual(t, f.currentNotice(t, f.held).RunnerCursor, int64(5))
	if !bytes.Equal(f.storedRunnerEntry(t, f.subscription.InboxID), boundary) {
		t.Error("a replay changed the boundary entry")
	}

	// A read that began before the committed cursor and went past it.
	f.receipt(t, f.held, f.notice, unrelatedEntry(7), 3)
	requireEqual(t, f.currentNotice(t, f.held).RunnerCursor, int64(7))
}

func TestCheckpointSkipsRunnerHistoryOnlyBeforeTheFirstAttempt(t *testing.T) {
	t.Run("from a high baseline the receipts follow", func(t *testing.T) {
		f := newNoticeFixture(t, 2)
		tail := unrelatedEntry(100_000)
		if err := f.CheckpointBeforeAttempt(t.Context(), f.held, f.notice, tail); err != nil {
			t.Fatal(err)
		}
		requireEqual(t, f.currentNotice(t, f.held).RunnerCursor, int64(100_000))
		wire, err := marshalEntry(tail)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(f.storedRunnerEntry(t, f.subscription.InboxID), wire) {
			t.Error("the checkpoint did not keep the entry it stopped at")
		}

		// The checkpoint is a boundary like any receipt's.
		f.receipt(t, f.held, f.notice, tail, 99_999)
		requireInvalidHistory(t, f.Receipt(t.Context(), f.held, f.notice, failedEntry(100_000, "another"), 99_999))
		f.receipt(t, f.held, f.notice, admittedEntry(100_001, f.notice.CommandID.String(), f.notice.Text), 100_000)
		got := f.currentNotice(t, f.held)
		requireEqual(t, got.RunnerCursor, int64(100_001))
		requireEqual(t, got.Admitted, true)
	})

	t.Run("it must advance", func(t *testing.T) {
		f := newNoticeFixture(t, 2)
		if err := f.CheckpointBeforeAttempt(t.Context(), f.held, f.notice, unrelatedEntry(5)); err != nil {
			t.Fatal(err)
		}
		for _, cursor := range []uint64{5, 3} {
			requireInvalidHistory(t, f.CheckpointBeforeAttempt(t.Context(), f.held, f.notice, unrelatedEntry(cursor)))
		}
		requireEqual(t, f.currentNotice(t, f.held).RunnerCursor, int64(5))
	})

	t.Run("not after an attempt", func(t *testing.T) {
		f := newNoticeFixture(t, 2)
		if attempt, err := f.Attempt(t.Context(), f.held, f.notice); err != nil || !attempt {
			t.Fatalf("Attempt = %v, %v", attempt, err)
		}
		requireInvalidHistory(t, f.CheckpointBeforeAttempt(t.Context(), f.held, f.notice, unrelatedEntry(5)))
		requireEqual(t, f.currentNotice(t, f.held).RunnerCursor, int64(0))
	})
}

func TestAReplacedNoticeCanNoLongerBeWrittenTo(t *testing.T) {
	f := newNoticeFixture(t, 1)
	old := f.notice
	if err := f.DeliveryFailed(t.Context(), f.held, old, "failed"); err != nil {
		t.Fatal(err)
	}
	f.addEvents(t, f.subscription, 2, 2)
	current := f.prepareNotice(t, f.held)

	writes := map[string]func() error{
		"Receipt": func() error {
			return f.Receipt(t.Context(), f.held, old, confirmedEntry(5, old.CommandID.String()), 0)
		},
		"Attempt":        func() error { _, err := f.Attempt(t.Context(), f.held, old); return err },
		"DeliveryFailed": func() error { return f.DeliveryFailed(t.Context(), f.held, old, "again") },
		"CheckpointBeforeAttempt": func() error {
			return f.CheckpointBeforeAttempt(t.Context(), f.held, old, unrelatedEntry(5))
		},
	}
	for name, write := range writes {
		var invalid *InvalidHistoryError
		if err := write(); err == nil || errors.As(err, &invalid) {
			t.Errorf("%s of the replaced notice = %v, want an error that is not about history", name, err)
		}
	}
	requireEqual(t, f.currentNotice(t, f.held), current)
}
