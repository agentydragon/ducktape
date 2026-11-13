package history

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// ChangesHeartbeat is how often a quiet decision watch gets a heartbeat.
const ChangesHeartbeat = 15 * time.Second

const (
	maxNotificationPayload = 7000 // PostgreSQL limits NOTIFY payloads to just under 8 KiB.
	maxNotificationRecords = 1000 // The configured decision writer's largest batch.
	maxNotificationQueue   = 2048
	maxSubscriberQueue     = 16
)

var (
	errChangeFeedOverflow = errors.New("decision change feed subscriber overflow")
	errChangeFeedEnded    = errors.New("decision change feed ended")
)

// Change is one of the events that can update a subject's bounded decision window.
type Change interface{ decisionChange() }

// Initial asks the reader to load its bounded window after the subscription is active.
type Initial struct{}

func (Initial) decisionChange() {}

// Events names rows committed for this subject since the preceding update.
type Events struct{ EventIDs []uuid.UUID }

func (Events) decisionChange() {}

// Heartbeat lets a reader expire old window entries after checking that history remains readable.
type Heartbeat struct{}

func (Heartbeat) decisionChange() {}

// Changes is one shared PostgreSQL listener per process. Each active filter has a bounded queue;
// a queue overflow or lost listener ends that filter's stream so its next connection can resnapshot.
type Changes struct {
	dsn       string
	heartbeat time.Duration

	mu       sync.Mutex
	listener *listenerState
}

// subjectKey is a decision.Subject, or the unauthenticated, as a comparable key: a ServiceAccount
// sets namespace and name, an Identity identity, the unauthenticated only unauthenticated.
type subjectKey struct {
	namespace       string
	name            string
	identity        string
	unauthenticated bool
}

type changeSubscription struct {
	key    subjectKey
	events chan changeEvent
	done   chan struct{}
	err    error
}

type changeEvent struct {
	eventIDs []uuid.UUID
}

type listenerState struct {
	conn          listenerConnection
	notifications chan *pq.Notification
	stop          chan struct{}
	done          chan struct{}
	stopping      bool
	subscribers   map[*changeSubscription]struct{}
	bySubject     map[subjectKey]map[*changeSubscription]struct{}
}

type listenerConnection interface {
	Listen(string) (bool, error)
	Close() error
	Err() error
}

// notifiedSubject is one subject's events in a notification every replica decodes. A replica
// predating Identity fails its decision watches on a notification naming one; none is written
// before a workload runs as an Identity (SANDBOX_IDENTITY_SPEC, agentplane/plans/task_dag.md), which
// a later release brings.
type notifiedSubject struct {
	Namespace       string   `json:"namespace,omitempty"`
	Name            string   `json:"name,omitempty"`
	Identity        string   `json:"identity,omitempty"`
	Unauthenticated bool     `json:"unauthenticated,omitempty"`
	EventIDs        []string `json:"event_ids"`
}

type changeNotification struct {
	BatchID  string            `json:"batch_id"`
	Final    bool              `json:"final"`
	Overflow bool              `json:"overflow,omitempty"`
	Subjects []notifiedSubject `json:"subjects,omitempty"`
}

type pendingChanges struct {
	batchID string
	count   int
	byKey   map[subjectKey][]uuid.UUID
}

// NewChanges follows committed decisions from any replica. It opens its listener lazily for the
// first watch, so an unused admin endpoint does not hold a database connection.
func NewChanges(databaseURL string, heartbeat time.Duration) (*Changes, error) {
	dsn, err := connectionString(databaseURL, "agentplane-egress-decision-changes")
	if err != nil {
		return nil, err
	}
	if heartbeat <= 0 {
		return nil, fmt.Errorf("change heartbeat must be positive: %s", heartbeat)
	}
	return &Changes{dsn: dsn, heartbeat: heartbeat}, nil
}

// Follow subscribes before its initial callback, then invokes changed only for this subject's
// committed event IDs and quiet heartbeats.
func (c *Changes) Follow(ctx context.Context, subject decision.Subject, changed func(Change) error) error {
	subscription, err := c.subscribe(ctx, subject)
	if err != nil {
		return err
	}
	defer c.unsubscribe(subscription)
	if err := changed(Initial{}); err != nil {
		return err
	}
	timer := time.NewTimer(c.heartbeat)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-subscription.done:
			if subscription.err != nil {
				return subscription.err
			}
			return errChangeFeedEnded
		case event := <-subscription.events:
			if err := changed(Events{EventIDs: event.eventIDs}); err != nil {
				return err
			}
			resetTimer(timer, c.heartbeat)
		case <-timer.C:
			if err := changed(Heartbeat{}); err != nil {
				return err
			}
			timer.Reset(c.heartbeat)
		}
	}
}

func (c *Changes) subscribe(ctx context.Context, subject decision.Subject) (*changeSubscription, error) {
	key := keyOf(subject)
	for {
		c.mu.Lock()
		state := c.listener
		if state != nil && state.stopping {
			done := state.done
			c.mu.Unlock()
			select {
			case <-done:
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		if state == nil {
			notifications := make(chan *pq.Notification, maxNotificationQueue)
			conn, err := pq.NewListenerConn(c.dsn, notifications)
			if err != nil {
				c.mu.Unlock()
				return nil, err
			}
			if _, err := conn.Listen(decisionsChannel); err != nil {
				_ = conn.Close()
				c.mu.Unlock()
				return nil, err
			}
			state = &listenerState{
				conn: conn, notifications: notifications, stop: make(chan struct{}), done: make(chan struct{}),
				subscribers: make(map[*changeSubscription]struct{}),
				bySubject:   make(map[subjectKey]map[*changeSubscription]struct{}),
			}
			c.listener = state
			go c.listen(state)
		}
		subscription := &changeSubscription{
			key: key, events: make(chan changeEvent, maxSubscriberQueue), done: make(chan struct{}),
		}
		state.subscribers[subscription] = struct{}{}
		if state.bySubject[key] == nil {
			state.bySubject[key] = make(map[*changeSubscription]struct{})
		}
		state.bySubject[key][subscription] = struct{}{}
		c.mu.Unlock()
		return subscription, nil
	}
}

func (c *Changes) unsubscribe(subscription *changeSubscription) {
	c.mu.Lock()
	defer c.mu.Unlock()
	state := c.listener
	if state == nil || state.stopping {
		return
	}
	if _, ok := state.subscribers[subscription]; !ok {
		return
	}
	c.removeSubscriberLocked(state, subscription, nil)
	if len(state.subscribers) == 0 {
		state.stopping = true
		close(state.stop)
	}
}

func (c *Changes) listen(state *listenerState) {
	defer close(state.done)
	var pending *pendingChanges
	deadline := time.NewTicker(c.heartbeat)
	defer deadline.Stop()
	for {
		select {
		case <-state.stop:
			_ = state.conn.Close()
			for range state.notifications {
			}
			c.clearListener(state)
			return
		case notification, open := <-state.notifications:
			if !open || notification == nil {
				c.failListener(state, fmt.Errorf("listen connection lost: %w", state.conn.Err()))
				return
			}
			if len(state.notifications) == cap(state.notifications) {
				c.failListener(state, errors.New("decision notification queue overflow"))
				return
			}
			payload, err := decodeNotification(notification.Extra)
			if err != nil {
				c.failListener(state, err)
				return
			}
			if payload.Overflow {
				if pending != nil || !payload.Final || len(payload.Subjects) != 0 {
					c.failListener(state, errors.New("invalid decision change overflow notification"))
					return
				}
				c.failListener(state, errors.New("decision change notification exceeded its safe bound"))
				return
			}
			if pending == nil {
				pending = &pendingChanges{batchID: payload.BatchID, byKey: make(map[subjectKey][]uuid.UUID)}
			} else if pending.batchID != payload.BatchID {
				c.failListener(state, errors.New("incomplete decision change notification batch"))
				return
			}
			for _, subjectChanges := range payload.Subjects {
				key := keyFromNotification(subjectChanges)
				pending.byKey[key] = append(pending.byKey[key], parseEventIDs(subjectChanges.EventIDs)...)
				pending.count += len(subjectChanges.EventIDs)
				if pending.count > maxNotificationRecords {
					c.failListener(state, errors.New("decision change notification batch exceeded its bound"))
					return
				}
			}
			if payload.Final {
				c.broadcast(state, changeEvent{}, pending.byKey)
				pending = nil
			}
		case <-deadline.C:
			if pending != nil {
				c.failListener(state, errors.New("incomplete decision change notification batch"))
				return
			}
		}
	}
}

func (c *Changes) broadcast(state *listenerState, event changeEvent, changes map[subjectKey][]uuid.UUID) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.listener != state || state.stopping {
		return
	}
	for key, eventIDs := range changes {
		for subscription := range state.bySubject[key] {
			c.enqueueLocked(state, subscription, changeEvent{eventIDs: eventIDs})
		}
	}
}

func (c *Changes) enqueueLocked(state *listenerState, subscription *changeSubscription, event changeEvent) {
	select {
	case subscription.events <- event:
	default:
		c.removeSubscriberLocked(state, subscription, errChangeFeedOverflow)
		if len(state.subscribers) == 0 && !state.stopping {
			state.stopping = true
			close(state.stop)
		}
	}
}

func (c *Changes) removeSubscriberLocked(state *listenerState, subscription *changeSubscription, err error) {
	delete(state.subscribers, subscription)
	delete(state.bySubject[subscription.key], subscription)
	if len(state.bySubject[subscription.key]) == 0 {
		delete(state.bySubject, subscription.key)
	}
	subscription.err = err
	close(subscription.done)
}

// failListener fails active watches, closes the connection, and drains the driver channel
// until pq can observe the close. The queue never drops notifications or grows without bound.
func (c *Changes) failListener(state *listenerState, err error) {
	c.mu.Lock()
	if c.listener == state && !state.stopping {
		state.stopping = true
		for subscription := range state.subscribers {
			c.removeSubscriberLocked(state, subscription, err)
		}
	}
	c.mu.Unlock()
	_ = state.conn.Close()
	for range state.notifications {
	}
	c.clearListener(state)
}

func (c *Changes) clearListener(state *listenerState) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.listener == state {
		c.listener = nil
	}
}

func keyOf(subject decision.Subject) subjectKey {
	switch subject := subject.(type) {
	case nil:
		return subjectKey{unauthenticated: true}
	case decision.ServiceAccount:
		return subjectKey{namespace: subject.Namespace, name: subject.Name}
	case decision.Identity:
		return subjectKey{identity: subject.Name}
	default:
		panic(fmt.Sprintf("unknown decision subject %T", subject))
	}
}

func keyFromNotification(change notifiedSubject) subjectKey {
	return subjectKey{
		namespace: change.Namespace, name: change.Name, identity: change.Identity, unauthenticated: change.Unauthenticated,
	}
}

func decodeNotification(raw string) (changeNotification, error) {
	if len(raw) == 0 || len(raw) > maxNotificationPayload {
		return changeNotification{}, errors.New("invalid decision change notification size")
	}
	var payload changeNotification
	if err := json.Unmarshal([]byte(raw), &payload); err != nil {
		return changeNotification{}, fmt.Errorf("invalid decision change notification: %w", err)
	}
	if _, err := uuid.Parse(payload.BatchID); err != nil {
		return changeNotification{}, errors.New("invalid decision change notification batch ID")
	}
	if payload.Overflow {
		if !payload.Final || len(payload.Subjects) != 0 {
			return changeNotification{}, errors.New("invalid decision change overflow notification")
		}
		return payload, nil
	}
	if len(payload.Subjects) == 0 {
		return changeNotification{}, errors.New("decision change notification has no subjects")
	}
	for _, change := range payload.Subjects {
		switch {
		case change.Unauthenticated:
			if change.Namespace != "" || change.Name != "" || change.Identity != "" {
				return changeNotification{}, errors.New("invalid unauthenticated decision change subject")
			}
		case change.Identity != "":
			if change.Namespace != "" || change.Name != "" {
				return changeNotification{}, errors.New("decision change subject names two kinds")
			}
		case change.Namespace == "" || change.Name == "":
			return changeNotification{}, errors.New("incomplete decision change subject")
		}
		if len(change.EventIDs) == 0 {
			return changeNotification{}, errors.New("decision change subject has no event IDs")
		}
		for _, eventID := range change.EventIDs {
			if _, err := uuid.Parse(eventID); err != nil {
				return changeNotification{}, errors.New("invalid decision change event ID")
			}
		}
	}
	return payload, nil
}

func parseEventIDs(eventIDs []string) []uuid.UUID {
	parsed := make([]uuid.UUID, 0, len(eventIDs))
	for _, eventID := range eventIDs {
		id, _ := uuid.Parse(eventID) // decodeNotification validated every ID before dispatch.
		parsed = append(parsed, id)
	}
	return parsed
}

// notificationPayloads carries only subject identities and event IDs. The explicit size cap leaves
// room below PostgreSQL's NOTIFY limit; every chunk is committed atomically with the decision rows.
func notificationPayloads(records []decision.Record) []string {
	batchID := uuid.NewString()
	if len(records) > maxNotificationRecords {
		return overflowPayload(batchID)
	}
	bySubject := make(map[subjectKey][]string)
	seen := make(map[uuid.UUID]struct{}, len(records))
	for _, record := range records {
		if _, ok := seen[record.EventID]; ok {
			continue
		}
		seen[record.EventID] = struct{}{}
		key := keyOf(record.Subject)
		bySubject[key] = append(bySubject[key], record.EventID.String())
	}
	keys := make([]subjectKey, 0, len(bySubject))
	for key := range bySubject {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].unauthenticated != keys[j].unauthenticated {
			return !keys[i].unauthenticated
		}
		if keys[i].namespace != keys[j].namespace {
			return keys[i].namespace < keys[j].namespace
		}
		if keys[i].name != keys[j].name {
			return keys[i].name < keys[j].name
		}
		return keys[i].identity < keys[j].identity
	})

	groups := make([]notifiedSubject, 0, len(records))
	for _, key := range keys {
		eventIDs := bySubject[key]
		for len(eventIDs) > 0 {
			count := min(len(eventIDs), 150)
			group := notifiedSubject{
				Namespace: key.namespace, Name: key.name, Identity: key.identity, Unauthenticated: key.unauthenticated,
				EventIDs: eventIDs[:count],
			}
			candidate := changeNotification{BatchID: batchID, Subjects: []notifiedSubject{group}}
			encoded, _ := json.Marshal(candidate)
			for len(encoded) > maxNotificationPayload && count > 1 {
				count /= 2
				group.EventIDs = eventIDs[:count]
				candidate.Subjects[0] = group
				encoded, _ = json.Marshal(candidate)
			}
			if len(encoded) > maxNotificationPayload {
				return overflowPayload(batchID)
			}
			groups = append(groups, group)
			eventIDs = eventIDs[count:]
		}
	}

	chunks := make([]changeNotification, 0)
	current := changeNotification{BatchID: batchID}
	for _, group := range groups {
		candidate := current
		candidate.Subjects = append(append([]notifiedSubject(nil), current.Subjects...), group)
		encoded, _ := json.Marshal(candidate)
		if len(encoded) > maxNotificationPayload && len(current.Subjects) > 0 {
			chunks = append(chunks, current)
			current = changeNotification{BatchID: batchID, Subjects: []notifiedSubject{group}}
			continue
		}
		if len(encoded) > maxNotificationPayload {
			return overflowPayload(batchID)
		}
		current = candidate
	}
	if len(current.Subjects) > 0 {
		chunks = append(chunks, current)
	}
	encoded := make([]string, len(chunks))
	for i, chunk := range chunks {
		chunk.Final = i == len(chunks)-1
		payload, _ := json.Marshal(chunk)
		if len(payload) > maxNotificationPayload {
			return overflowPayload(batchID)
		}
		encoded[i] = string(payload)
	}
	return encoded
}

func overflowPayload(batchID string) []string {
	payload, _ := json.Marshal(changeNotification{BatchID: batchID, Final: true, Overflow: true})
	return []string{string(payload)}
}

func resetTimer(timer *time.Timer, duration time.Duration) {
	if !timer.Stop() {
		select {
		case <-timer.C:
		default:
		}
	}
	timer.Reset(duration)
}
