package admin

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sort"
	"sync"
	"time"

	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/api/admin/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/google/uuid"
)

const windowSubscriberQueue = 16

var errWindowOverflow = errors.New("decision window subscriber overflow")

type windowUpdate struct {
	delta     *egressadminpb.DecisionDelta
	heartbeat bool
}

type windowSubscriber struct {
	updates chan windowUpdate
	done    chan struct{}
	err     error
}

type subjectWindow struct {
	// subject keys the window; nil is the refusals that never authenticated.
	subject     decision.Subject
	records     []decision.Record
	ready       chan struct{}
	initialized bool
	waiters     int
	subscribers map[*windowSubscriber]struct{}
	cancel      context.CancelFunc
	err         error
}

type decisionWindows struct {
	decisions Decisions
	changes   Changes
	window    history.Window

	mu        sync.Mutex
	active    map[decision.Subject]*subjectWindow
	probeGate chan struct{}
	lastProbe time.Time
}

func newDecisionWindows(decisions Decisions, changes Changes) *decisionWindows {
	windows := &decisionWindows{
		decisions: decisions, changes: changes, window: decisions.Window(),
		active: make(map[decision.Subject]*subjectWindow), probeGate: make(chan struct{}, 1),
	}
	windows.probeGate <- struct{}{}
	return windows
}

func (w *decisionWindows) subscribe(ctx context.Context, subject decision.Subject) (*windowSubscriber, []*egressadminpb.Decision, error) {
	w.mu.Lock()
	state := w.active[subject]
	if state == nil {
		feedCtx, cancel := context.WithCancel(context.Background())
		state = &subjectWindow{
			subject: subject, ready: make(chan struct{}),
			subscribers: make(map[*windowSubscriber]struct{}), cancel: cancel,
		}
		w.active[subject] = state
		go w.follow(feedCtx, state)
	}
	state.waiters++
	ready := state.ready
	w.mu.Unlock()

	select {
	case <-ready:
	case <-ctx.Done():
		w.removeWaiter(state)
		return nil, nil, ctx.Err()
	}

	w.mu.Lock()
	defer w.mu.Unlock()
	if state.err != nil {
		state.waiters--
		if state.initialized {
			subscriber := &windowSubscriber{updates: make(chan windowUpdate, windowSubscriberQueue), done: make(chan struct{}), err: state.err}
			close(subscriber.done)
			return subscriber, decisionsOf(state.records), nil
		}
		w.stopIfUnusedLocked(state)
		return nil, nil, state.err
	}
	if w.active[subject] != state {
		state.waiters--
		return nil, nil, errors.New("decision window ended while subscribing")
	}
	state.waiters--
	subscriber := &windowSubscriber{updates: make(chan windowUpdate, windowSubscriberQueue), done: make(chan struct{})}
	state.subscribers[subscriber] = struct{}{}
	return subscriber, decisionsOf(state.records), nil
}

func (w *decisionWindows) unsubscribe(subject decision.Subject, subscriber *windowSubscriber) {
	w.mu.Lock()
	defer w.mu.Unlock()
	state := w.active[subject]
	if state == nil {
		return
	}
	if _, ok := state.subscribers[subscriber]; ok {
		delete(state.subscribers, subscriber)
		close(subscriber.done)
	}
	w.stopIfUnusedLocked(state)
}

func (w *decisionWindows) removeWaiter(state *subjectWindow) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.active[state.subject] != state {
		return
	}
	state.waiters--
	w.stopIfUnusedLocked(state)
}

func (w *decisionWindows) stopIfUnusedLocked(state *subjectWindow) {
	if state.waiters == 0 && len(state.subscribers) == 0 && w.active[state.subject] == state {
		delete(w.active, state.subject)
		state.cancel()
	}
}

func (w *decisionWindows) follow(ctx context.Context, state *subjectWindow) {
	err := w.changes.Follow(ctx, state.subject, func(change history.Change) error {
		switch change := change.(type) {
		case history.Initial:
			records, err := w.decisions.Recent(ctx, state.subject)
			if err != nil {
				return err
			}
			if err := validateWindowRecords(state.subject, records); err != nil {
				return err
			}
			w.initialize(state, records)
		case history.Events:
			records, err := w.decisions.ByEventIDs(ctx, change.EventIDs)
			if err != nil {
				return err
			}
			if err := validateWindowRecords(state.subject, records); err != nil {
				return err
			}
			w.apply(state, records, false)
		case history.Heartbeat:
			if err := w.verifyReadable(ctx); err != nil {
				return err
			}
			w.apply(state, nil, true)
		default:
			return errors.New("unknown decision history change")
		}
		return nil
	})
	if err == nil && ctx.Err() == nil {
		err = errors.New("decision history change feed ended")
	}
	w.fail(state, err)
}

// verifyReadable coalesces quiet-feed probes across all active subjects in this process. A watch
// heartbeat therefore still proves both LISTEN and a history read succeeded, without one query per
// watcher or a reread of every subject's bounded window.
func (w *decisionWindows) verifyReadable(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-w.probeGate:
	}
	defer func() { w.probeGate <- struct{}{} }()
	if !w.lastProbe.IsZero() && time.Since(w.lastProbe) < history.ChangesHeartbeat {
		return nil
	}
	if err := w.decisions.Readable(ctx); err != nil {
		return err
	}
	w.lastProbe = time.Now()
	return nil
}

func (w *decisionWindows) initialize(state *subjectWindow, records []decision.Record) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.active[state.subject] != state || state.err != nil {
		return
	}
	state.records = boundedWindow(records, nil, w.window)
	state.initialized = true
	close(state.ready)
}

func (w *decisionWindows) apply(state *subjectWindow, added []decision.Record, heartbeat bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.active[state.subject] != state || !state.initialized || state.err != nil {
		return
	}
	previous := state.records
	current := boundedWindow(previous, added, w.window)
	delta := decisionDelta(decisionsOf(previous), decisionsOf(current))
	state.records = current
	if len(delta.Added) == 0 && len(delta.RemovedEventIds) == 0 {
		if heartbeat {
			w.broadcastLocked(state, windowUpdate{heartbeat: true})
		}
		return
	}
	w.broadcastLocked(state, windowUpdate{delta: delta})
}

func (w *decisionWindows) broadcastLocked(state *subjectWindow, update windowUpdate) {
	for subscriber := range state.subscribers {
		select {
		case subscriber.updates <- update:
		default:
			delete(state.subscribers, subscriber)
			subscriber.err = errWindowOverflow
			close(subscriber.done)
		}
	}
	w.stopIfUnusedLocked(state)
}

func (w *decisionWindows) fail(state *subjectWindow, err error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.active[state.subject] != state {
		return
	}
	if err != nil && ctxIsCanceled(err) && len(state.subscribers) == 0 && state.waiters == 0 {
		delete(w.active, state.subject)
		return
	}
	state.err = err
	if !state.initialized {
		close(state.ready)
	}
	for subscriber := range state.subscribers {
		delete(state.subscribers, subscriber)
		subscriber.err = err
		close(subscriber.done)
	}
	delete(w.active, state.subject)
}

func boundedWindow(previous, added []decision.Record, window history.Window) []decision.Record {
	cutoff := time.Now().Add(-window.Retention)
	byID := make(map[uuid.UUID]decision.Record, len(previous)+len(added))
	for _, record := range previous {
		if !record.At.Before(cutoff) {
			byID[record.EventID] = record
		}
	}
	for _, record := range added {
		if !record.At.Before(cutoff) {
			byID[record.EventID] = record
		}
	}
	current := make([]decision.Record, 0, len(byID))
	for _, record := range byID {
		current = append(current, record)
	}
	sort.Slice(current, func(i, j int) bool {
		if current[i].At.Equal(current[j].At) {
			return current[i].EventID.String() < current[j].EventID.String()
		}
		return current[i].At.Before(current[j].At)
	})
	if len(current) > window.Capacity {
		current = slices.Clone(current[len(current)-window.Capacity:])
	}
	return current
}

func validateWindowRecords(subject decision.Subject, records []decision.Record) error {
	for _, record := range records {
		if record.Subject != subject {
			return fmt.Errorf("decision feed returned an event for a different subject")
		}
	}
	return nil
}

func ctxIsCanceled(err error) bool {
	return errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded)
}
