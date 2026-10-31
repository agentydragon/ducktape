package adaptertest

import (
	"context"
	"encoding/json"
	"errors"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
)

type Journal struct {
	TurnEventsError error
	Items           map[string]harness.ObservedItem
	Events          []*eventlogpb.EventEntry
	Pending         []harness.CommandRecord
	Requests        []harness.NativeRequest
	Seen            map[string]bool
	Blocks          map[string]int
}

func NewJournal() *Journal {
	return &Journal{
		Items:  make(map[string]harness.ObservedItem),
		Seen:   make(map[string]bool),
		Blocks: make(map[string]int),
	}
}

func (j *Journal) ObservedItems(context.Context, string) (map[string]harness.ObservedItem, error) {
	return j.Items, nil
}

func (j *Journal) RecoveryTurns(context.Context) ([]string, error) { return nil, nil }

func (j *Journal) TurnEvents(context.Context, string) ([]*eventlogpb.EventEntry, error) {
	return j.Events, j.TurnEventsError
}

func (j *Journal) PendingCommandRecords(context.Context) ([]harness.CommandRecord, error) {
	return j.Pending, nil
}

func (j *Journal) PendingNativeRequests(context.Context) ([]harness.NativeRequest, error) {
	return j.Requests, nil
}

func (j *Journal) Since(_ context.Context, cursor uint64, limit int) ([]*eventlogpb.EventEntry, error) {
	entries := make([]*eventlogpb.EventEntry, 0, limit)
	for _, entry := range j.Events {
		if entry.GetCursor() > cursor {
			entries = append(entries, entry)
			if len(entries) == limit {
				break
			}
		}
	}
	return entries, nil
}

func (j *Journal) LastCursor() uint64 {
	var last uint64
	for _, entry := range j.Events {
		if entry.GetCursor() > last {
			last = entry.GetCursor()
		}
	}
	return last
}

func (j *Journal) RememberAdapterItem(_ context.Context, adapterID, itemID string) (bool, error) {
	key := adapterID + "/" + itemID
	if j.Seen[key] {
		return false, nil
	}
	j.Seen[key] = true
	return true, nil
}

func (j *Journal) HasAdapterItem(_ context.Context, adapterID, itemID string) (bool, error) {
	return j.Seen[adapterID+"/"+itemID], nil
}

func (j *Journal) NextAdapterBlock(_ context.Context, adapterID, messageID string) (int, error) {
	key := adapterID + "/" + messageID
	index := j.Blocks[key]
	j.Blocks[key]++
	return index, nil
}

type Request struct {
	ID         string
	Kind       harness.NativeRequestKind
	CommandIDs []string
}

type Confirmation struct {
	Value   harness.UserMessageConfirmation
	Sources []uint64
}

type Host struct {
	SessionState   harness.SessionSnapshot
	JournalState   *Journal
	NativeWrites   []json.RawMessage
	Events         []*eventpb.Event
	EventSources   [][]uint64
	Confirmations  []Confirmation
	Noops          []string
	Failures       []string
	NativeRequests []Request
}

func NewHost() *Host {
	return &Host{
		SessionState: harness.SessionSnapshot{
			Directory: "/tmp/session", Cwd: "/workspace", Model: "test-model",
			ActiveTurnID: "turn-root", NativeSessionID: "thread-root", Resuming: true,
		},
		JournalState: NewJournal(),
	}
}

func (h *Host) Session() harness.SessionSnapshot { return h.SessionState }
func (h *Host) Journal() harness.AdapterJournal  { return h.JournalState }
func (h *Host) RecordNativeRequest(_ context.Context, id string, kind harness.NativeRequestKind, commandIDs []string) error {
	h.NativeRequests = append(h.NativeRequests, Request{ID: id, Kind: kind, CommandIDs: append([]string(nil), commandIDs...)})
	return nil
}

func (h *Host) SendNative(_ context.Context, frame json.RawMessage) error {
	h.NativeWrites = append(h.NativeWrites, append(json.RawMessage(nil), frame...))
	return nil
}

func (h *Host) RequestNativeHandshake(context.Context, json.RawMessage, func(json.RawMessage) bool) (harness.NativeReceipt, error) {
	return harness.NativeReceipt{}, errors.New("unexpected native request in adapter test")
}

func (h *Host) Emit(_ context.Context, event *eventpb.Event, sources []uint64) error {
	h.Events = append(h.Events, event)
	h.EventSources = append(h.EventSources, append([]uint64(nil), sources...))
	if turn := event.GetTurnStarted(); turn != nil {
		h.SessionState.ActiveTurnID = turn.TurnId
	}
	if event.GetTurnCompleted() != nil {
		h.SessionState.ActiveTurnID = ""
	}
	return nil
}

func (h *Host) ConfirmUserMessage(_ context.Context, value harness.UserMessageConfirmation, sources []uint64) error {
	h.Confirmations = append(h.Confirmations, Confirmation{Value: value, Sources: append([]uint64(nil), sources...)})
	return nil
}

func (h *Host) ModelChanged(_ context.Context, _, model string, _ []uint64) error {
	h.SessionState.Model = model
	return nil
}

func (h *Host) ReasoningEffortChanged(_ context.Context, _ string, effort string, _ []uint64) error {
	h.SessionState.ReasoningEffort = effort
	return nil
}

func (h *Host) FailCommand(_ context.Context, commandID, _ string, _ []uint64) error {
	h.Failures = append(h.Failures, commandID)
	return nil
}

func (h *Host) NoopCommand(_ context.Context, commandID, _ string, _ []uint64) error {
	h.Noops = append(h.Noops, commandID)
	return nil
}
