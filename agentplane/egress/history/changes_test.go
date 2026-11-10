package history

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestConnectionStringBoundsConnectsUnlessTheURLSaysOtherwise(t *testing.T) {
	for _, test := range []struct{ url, timeout, application string }{
		{"postgresql://test-user:test-password@db.test/history", "2", "test-app"},
		{"postgres://db.test/history?connect_timeout=9&application_name=other", "9", "other"},
	} {
		dsn, err := connectionString(test.url, "test-app")
		if err != nil {
			t.Fatal(err)
		}
		parsed, err := url.Parse(dsn)
		if err != nil {
			t.Fatal(err)
		}
		query := parsed.Query()
		if query.Get("connect_timeout") != test.timeout || query.Get("application_name") != test.application {
			t.Errorf("%s -> %s", test.url, dsn)
		}
	}
}

func TestInvalidDatabaseURLNeverEchoesThePassword(t *testing.T) {
	for _, bad := range []string{
		"postgres://test-user:test-secret@db.test:not-a-port/history",
		"mysql://test-user:test-secret@db.test/history",
	} {
		_, err := connectionString(bad, "test-app")
		if err == nil {
			t.Fatalf("%q accepted", bad)
		}
		if strings.Contains(err.Error(), "test-secret") {
			t.Errorf("error leaks the password: %v", err)
		}
	}
}

func TestFollowFailsWithoutSubscribingWhenTheDatabaseIsDown(t *testing.T) {
	changes, err := NewChanges("postgresql://test-user:test-password@127.0.0.1:1/history?sslmode=disable", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	err = changes.Follow(ctx, nil, func(Change) error {
		t.Error("told the reader about changes without a connection")
		return nil
	})
	if err == nil || ctx.Err() != nil {
		t.Fatalf("Follow returned %v after %v", err, ctx.Err())
	}
}

func TestNotificationPayloadsChunkCommittedIDsWithoutDecisionDetails(t *testing.T) {
	records := make([]decision.Record, maxNotificationRecords)
	ids := make(map[uuid.UUID]bool, len(records))
	for i := range records {
		records[i] = testRecord(time.Now())
		records[i].EventID = uuid.NewSHA1(uuid.NameSpaceOID, []byte(fmt.Sprintf("event-%d", i)))
		records[i].Host = "secret-host.example"
		records[i].Method = "secret-method"
		records[i].Policy = stringPtr("secret-policy")
		ids[records[i].EventID] = false
	}

	payloads := notificationPayloads(records)
	if len(payloads) < 2 {
		t.Fatalf("got %d payloads for %d records, want bounded chunks", len(payloads), len(records))
	}
	batchID := ""
	for i, raw := range payloads {
		if len(raw) > maxNotificationPayload {
			t.Fatalf("payload %d is %d bytes, over %d", i, len(raw), maxNotificationPayload)
		}
		if strings.Contains(raw, "secret-host") || strings.Contains(raw, "secret-method") || strings.Contains(raw, "secret-policy") {
			t.Fatalf("payload leaked decision details: %s", raw)
		}
		payload, err := decodeNotification(raw)
		if err != nil {
			t.Fatalf("decode payload %d: %v", i, err)
		}
		if batchID == "" {
			batchID = payload.BatchID
		} else if payload.BatchID != batchID {
			t.Fatalf("payload %d has batch ID %q, want %q", i, payload.BatchID, batchID)
		}
		if payload.Final != (i == len(payloads)-1) {
			t.Fatalf("payload %d final=%t, want %t", i, payload.Final, i == len(payloads)-1)
		}
		for _, subject := range payload.Subjects {
			for _, rawID := range subject.EventIDs {
				id, _ := uuid.Parse(rawID)
				if _, ok := ids[id]; !ok || ids[id] {
					t.Fatalf("unexpected or duplicate event ID %s", rawID)
				}
				ids[id] = true
			}
		}
	}
	for id, seen := range ids {
		if !seen {
			t.Errorf("event ID %s was not notified", id)
		}
	}
}

func TestNotificationOverflowFailsClosedAndEmptyLegacyPayloadIsInvalid(t *testing.T) {
	tooMany := make([]decision.Record, maxNotificationRecords+1)
	for i := range tooMany {
		tooMany[i] = testRecord(time.Now())
	}
	if payloads := notificationPayloads(tooMany); len(payloads) != 1 {
		t.Fatalf("overflow emitted %d notifications, want one explicit marker", len(payloads))
	} else if payload, err := decodeNotification(payloads[0]); err != nil || !payload.Overflow {
		t.Fatalf("overflow payload = %+v, %v; want explicit overflow", payload, err)
	}
	for _, raw := range []string{"", `{"batch_id":"00000000-0000-0000-0000-000000000001","final":true,"full_refresh":true}`} {
		if _, err := decodeNotification(raw); err == nil {
			t.Errorf("accepted unsafe or legacy payload %q", raw)
		}
	}
}

func stringPtr(value string) *string { return &value }

type testListenerConnection struct {
	closed  chan struct{}
	once    sync.Once
	onClose func()
}

func (*testListenerConnection) Listen(string) (bool, error) { return true, nil }

func (c *testListenerConnection) Close() error {
	c.once.Do(func() {
		close(c.closed)
		if c.onClose != nil {
			c.onClose()
		}
	})
	return nil
}

func (*testListenerConnection) Err() error { return nil }

func TestFailListenerDrainsFullPQNotificationQueue(t *testing.T) {
	notifications := make(chan *pq.Notification, 1)
	notifications <- &pq.Notification{}
	connection := &testListenerConnection{closed: make(chan struct{})}
	key := subjectKey{namespace: "test", name: "workload"}
	subscription := &changeSubscription{key: key, events: make(chan changeEvent, 1), done: make(chan struct{})}
	state := &listenerState{
		conn: connection, notifications: notifications, subscribers: map[*changeSubscription]struct{}{subscription: {}},
		bySubject: map[subjectKey]map[*changeSubscription]struct{}{key: {subscription: {}}},
	}
	changes := &Changes{listener: state}

	// A full pq channel leaves its sender blocked until this listener drains it. The simulated driver
	// closes the notification channel only after the blocked send is accepted and the socket closes.
	blockedSender := make(chan struct{})
	senderDone := make(chan struct{})
	go func() {
		close(blockedSender)
		notifications <- &pq.Notification{}
		<-connection.closed
		close(notifications)
		close(senderDone)
	}()
	<-blockedSender

	changes.failListener(state, errors.New("test notification overflow"))
	select {
	case <-senderDone:
	case <-time.After(time.Second):
		t.Fatal("listener shutdown failed to drain and unblock pq's sender")
	}
	select {
	case <-subscription.done:
		if subscription.err == nil {
			t.Fatal("subscriber ended without an overflow error")
		}
	default:
		t.Fatal("listener failure did not end the subscriber")
	}
	if changes.listener != nil {
		t.Fatal("failed listener remained installed")
	}
}

func TestOverflowMarkerEndsTheChangeStream(t *testing.T) {
	notifications := make(chan *pq.Notification, 1)
	connection := &testListenerConnection{closed: make(chan struct{}), onClose: func() { close(notifications) }}
	key := subjectKey{namespace: "test", name: "workload"}
	subscription := &changeSubscription{key: key, events: make(chan changeEvent, maxSubscriberQueue), done: make(chan struct{})}
	state := &listenerState{
		conn: connection, notifications: notifications, stop: make(chan struct{}), done: make(chan struct{}),
		subscribers: map[*changeSubscription]struct{}{subscription: {}},
		bySubject:   map[subjectKey]map[*changeSubscription]struct{}{key: {subscription: {}}},
	}
	changes := &Changes{listener: state, heartbeat: time.Hour}
	finished := make(chan struct{})
	go func() {
		changes.listen(state)
		close(finished)
	}()
	tooMany := make([]decision.Record, maxNotificationRecords+1)
	for i := range tooMany {
		tooMany[i] = testRecord(time.Now())
	}
	notifications <- &pq.Notification{Extra: notificationPayloads(tooMany)[0]}

	select {
	case <-subscription.done:
		if subscription.err == nil {
			t.Fatal("overflow marker ended the subscriber without an error")
		}
	case <-time.After(time.Second):
		t.Fatal("overflow marker did not end the change stream")
	}
	select {
	case <-finished:
	case <-time.After(time.Second):
		t.Fatal("overflowed listener did not shut down")
	}
	if changes.listener != nil {
		t.Fatal("overflowed listener remained installed")
	}
}
