package push

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/SherClockHolmes/webpush-go"
	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

const (
	// ttlSeconds is how long a push service holds a push for a browser that is offline.
	ttlSeconds = 600
	// topic is every push's Topic: a push service holding undelivered pushes for an offline browser keeps only the
	// newest, so the browser wakes once to the current state instead of once per request.
	topic = "agentplane-actions"
	// lockClass is the first key of each subscription's advisory lock, whose second is the hash of its endpoint.
	// The two-key form never meets the database's one-key locks, such as the migrators'.
	lockClass = 0x5055_5348 // "PUSH"
	// unlockTimeout bounds giving a subscription's lock back once its delivery ended.
	unlockTimeout = 5 * time.Second
)

// Timing is delivery's intervals and bounds.
type Timing struct {
	// Sweep is how often a pass runs without a wakeup, which retries the pushes that failed.
	Sweep time.Duration
	// Retry is how long a pass that failed waits before the next.
	Retry time.Duration
	// Send bounds one push.
	Send time.Duration
}

// DefaultTiming is the Python service's.
func DefaultTiming() Timing {
	return Timing{Sweep: 30 * time.Second, Retry: 5 * time.Second, Send: 10 * time.Second}
}

// Notifier pushes to every subscription what changed among the requests awaiting a Decision since its browser was
// last told: one push per pass, which shows the newest request that came to await a Decision, or, when none did,
// retracts the newest that left. The browser's service worker rebuilds its one notification from the requests as they
// stand, so a push per request would only make a browser that was offline replay a burst.
//
// The rows are the authority. Any replica's Notifier delivers to any subscription, under the subscription's advisory
// lock, which another replica's pass skips; the lock outlives no session, so a replica that dies gives it back. A push
// is sent with no transaction open and recorded after, so a replica that dies in between sends it again, which the
// service worker's single notification absorbs. A push service that reports the subscription gone (404 or 410) has it
// deleted. Nothing is executed here.
type Notifier struct {
	db       *sql.DB
	requests *store.Store
	identity *Identity
	client   *http.Client
	// baseURL is the integration app's address, without a trailing slash.
	baseURL string
	timing  Timing
}

// NewNotifier delivers over transport, which reaches the push services.
func NewNotifier(db *sql.DB, requests *store.Store, identity *Identity, baseURL string, transport http.RoundTripper, timing Timing) (*Notifier, error) {
	for _, positive := range []time.Duration{timing.Sweep, timing.Retry, timing.Send} {
		if positive <= 0 {
			return nil, fmt.Errorf("push intervals and bounds must be positive: %+v", timing)
		}
	}
	return &Notifier{
		db: db, requests: requests, identity: identity, baseURL: strings.TrimRight(baseURL, "/"), timing: timing,
		// A push service's redirect is not followed: the endpoint is the only destination a subscription names.
		client: &http.Client{
			Transport:     observability.HTTPTransport(transport),
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}, nil
}

// Run delivers until ctx ends: when it starts, after each signal on changes, and every timing.Sweep. changes is
// signalled on each commit to a request (store.UpdatesChannel) and when the LISTEN connection returns, since a commit
// made while it was down is only in the tables.
func (n *Notifier) Run(ctx context.Context, changes <-chan struct{}) {
	for {
		// Clear before reading: a commit that races with the pass leaves a wakeup for the next wait.
		select {
		case <-changes:
		default:
		}
		wait := n.timing.Sweep
		if err := n.reconcile(ctx); err != nil {
			if ctx.Err() != nil {
				return
			}
			slog.WarnContext(ctx, "push delivery failed; retrying", "error", err)
			wait = n.timing.Retry
		}
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-changes:
		case <-timer.C:
		}
		timer.Stop()
	}
}

// reconcile runs one pass over every subscription.
func (n *Notifier) reconcile(ctx context.Context) error {
	rows, err := n.db.QueryContext(ctx, `SELECT endpoint FROM action_push_subscription ORDER BY endpoint`)
	if err != nil {
		return err
	}
	var endpoints []string
	for rows.Next() {
		var endpoint string
		if err := rows.Scan(&endpoint); err != nil {
			rows.Close()
			return err
		}
		endpoints = append(endpoints, endpoint)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	for _, endpoint := range endpoints {
		if err := n.deliver(ctx, endpoint); err != nil {
			return err
		}
	}
	return nil
}

// deliver pushes to the subscription what changed since its browser was last told, unless another pass holds it.
func (n *Notifier) deliver(ctx context.Context, endpoint string) (err error) {
	// The lock belongs to the session, so every statement of the delivery runs on one connection.
	conn, err := n.db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	var locked bool
	if err := conn.QueryRowContext(ctx, `SELECT pg_try_advisory_lock($1, hashtext($2))`, lockClass, endpoint).Scan(&locked); err != nil {
		return err
	}
	if !locked {
		return nil
	}
	defer func() {
		unlocking, cancel := context.WithTimeout(context.WithoutCancel(ctx), unlockTimeout)
		defer cancel()
		if _, unlockErr := conn.ExecContext(unlocking, `SELECT pg_advisory_unlock($1, hashtext($2))`, lockClass, endpoint); unlockErr != nil {
			// A session that may still hold the lock never goes back to the pool: closing it gives the lock back.
			_ = conn.Raw(func(any) error { return driver.ErrBadConn })
			err = errors.Join(err, fmt.Errorf("release the push lock: %w", unlockErr))
		}
	}()

	var subscription webpush.Subscription
	err = conn.QueryRowContext(ctx, `SELECT endpoint, p256dh, auth FROM action_push_subscription WHERE endpoint = $1`, endpoint).
		Scan(&subscription.Endpoint, &subscription.Keys.P256dh, &subscription.Keys.Auth)
	if errors.Is(err, sql.ErrNoRows) {
		// Removed since the pass listed it.
		return nil
	}
	if err != nil {
		return err
	}
	shown, err := n.shown(ctx, conn, endpoint)
	if err != nil {
		return err
	}
	ids := make([]uuid.UUID, 0, len(shown))
	for id := range shown {
		ids = append(ids, id)
	}
	requests, err := n.requests.Approvals(ctx, ids)
	if err != nil {
		return err
	}
	message, ledger := n.message(requests, shown)
	if message == nil {
		return nil
	}
	payload, err := json.Marshal(message)
	if err != nil {
		return err
	}
	switch n.send(ctx, &subscription, payload) {
	case gone:
		return deleteGone(ctx, conn, subscription)
	case sent:
		return record(ctx, conn, subscription, ledger)
	default:
		return nil
	}
}

// shown is the requests the subscription's browser was last shown, not yet retracted.
func (n *Notifier) shown(ctx context.Context, conn *sql.Conn, endpoint string) (map[uuid.UUID]bool, error) {
	rows, err := conn.QueryContext(ctx, `SELECT request_id FROM action_push_delivery WHERE endpoint = $1 AND kind = $2`, endpoint, kindShow)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	shown := map[uuid.UUID]bool{}
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		shown[id] = true
	}
	return shown, rows.Err()
}

// The ledger's kinds: what a browser was last told of a request.
const (
	kindShow    = "show"
	kindRetract = "retract"
)

// show and retract are the payloads the integration app's service worker reads
// (agentplane/app/frontend/service_worker/sw.ts).
type show struct {
	Kind        string `json:"kind"`
	ActionID    string `json:"action_id"`
	ActionGroup string `json:"action_group"`
	ActionName  string `json:"action_name"`
	Version     int64  `json:"version"`
	URL         string `json:"url"`
}

type retract struct {
	Kind     string `json:"kind"`
	ActionID string `json:"action_id"`
	// Outcome is the state the request left the wait for, in words.
	Outcome string `json:"outcome"`
}

// message is the push that tells a browser, which was shown the requests of shown, what changed among requests (oldest
// first), with the ledger entry of each request it covers; it is nil when nothing changed. A request that came to
// await a Decision is new; one shown that no longer awaits it is retracted.
func (n *Notifier) message(requests []store.ActionRequest, shown map[uuid.UUID]bool) (any, map[uuid.UUID]string) {
	var arrived, left []store.ActionRequest
	for _, request := range requests {
		switch pending := request.State == action.StateDecisionPending; {
		case pending && !shown[request.ID]:
			arrived = append(arrived, request)
		case !pending && shown[request.ID]:
			left = append(left, request)
		}
	}
	ledger := make(map[uuid.UUID]string, len(arrived)+len(left))
	for _, request := range arrived {
		ledger[request.ID] = kindShow
	}
	for _, request := range left {
		ledger[request.ID] = kindRetract
	}
	switch {
	case len(arrived) > 0:
		newest := arrived[len(arrived)-1]
		return show{
			Kind: kindShow, ActionID: newest.ID.String(), ActionGroup: newest.Action.Group, ActionName: newest.Action.Name,
			Version: newest.Version, URL: n.baseURL + "/#/actions/" + newest.ID.String(),
		}, ledger
	case len(left) > 0:
		newest := left[len(left)-1]
		return retract{
			Kind: kindRetract, ActionID: newest.ID.String(), Outcome: strings.ReplaceAll(string(newest.State), "_", " "),
		}, ledger
	default:
		return nil, nil
	}
}

// outcome is what became of a push.
type outcome int

const (
	// failed is a push to send again on a later pass.
	failed outcome = iota
	sent
	// gone is a subscription the push service no longer knows.
	gone
)

func (n *Notifier) send(ctx context.Context, subscription *webpush.Subscription, payload []byte) outcome {
	if err := n.identity.CheckEndpoint(subscription.Endpoint); err != nil {
		slog.WarnContext(ctx, "web push delivery refused: the endpoint is no longer a configured push service")
		return failed
	}
	sending, cancel := context.WithTimeout(ctx, n.timing.Send)
	defer cancel()
	response, err := webpush.SendNotificationWithContext(sending, payload, subscription, &webpush.Options{
		HTTPClient: n.client, Subscriber: n.identity.subscriber, Topic: topic, TTL: ttlSeconds,
		Urgency: webpush.UrgencyHigh, VAPIDPublicKey: n.identity.publicKey, VAPIDPrivateKey: n.identity.privateKey,
	})
	if err != nil {
		slog.WarnContext(ctx, "web push delivery failed", "error", err)
		return failed
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, response.Body)
	switch code := response.StatusCode; {
	case code == http.StatusNotFound || code == http.StatusGone:
		return gone
	case code >= 200 && code < 300:
		return sent
	default:
		slog.WarnContext(ctx, "web push delivery refused", "status", code)
		return failed
	}
}

// record writes what the browser was told into its ledger, unless the subscription was removed or registered with
// other keys since it was read: the next pass tells the browser it now names.
func record(ctx context.Context, conn *sql.Conn, subscription webpush.Subscription, ledger map[uuid.UUID]string) error {
	ids, kinds := make(pq.StringArray, 0, len(ledger)), make(pq.StringArray, 0, len(ledger))
	for id, kind := range ledger {
		ids, kinds = append(ids, id.String()), append(kinds, kind)
	}
	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	err = tx.QueryRowContext(ctx, `
		SELECT endpoint FROM action_push_subscription WHERE endpoint = $1 AND p256dh = $2 AND auth = $3 FOR SHARE`,
		subscription.Endpoint, subscription.Keys.P256dh, subscription.Keys.Auth).Scan(new(string))
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO action_push_delivery (request_id, endpoint, kind)
		SELECT request_id, $1, kind FROM unnest($2::uuid[], $3::text[]) AS told (request_id, kind)
		ON CONFLICT (request_id, endpoint) DO UPDATE SET kind = EXCLUDED.kind`,
		subscription.Endpoint, ids, kinds); err != nil {
		return err
	}
	return tx.Commit()
}

// deleteGone deletes the subscription the push service reported gone, with its ledger, unless it was registered with
// other keys since it was read.
func deleteGone(ctx context.Context, conn *sql.Conn, subscription webpush.Subscription) error {
	_, err := conn.ExecContext(ctx, `
		WITH deleted AS (
			DELETE FROM action_push_subscription WHERE endpoint = $1 AND p256dh = $2 AND auth = $3 RETURNING endpoint)
		SELECT pg_notify($4, '') FROM deleted`,
		subscription.Endpoint, subscription.Keys.P256dh, subscription.Keys.Auth, UpdatesChannel)
	return err
}
