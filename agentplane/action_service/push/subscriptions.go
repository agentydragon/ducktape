package push

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go"
)

// UpdatesChannel is notified, with no payload, in every commit that adds, changes or removes a subscription.
const UpdatesChannel = "agentplane_push_updates"

var (
	// ErrNotFound is a subscription the operator does not hold.
	ErrNotFound = errors.New("push subscription not found")
	// ErrRegisteredElsewhere is an endpoint another operator registered.
	ErrRegisteredElsewhere = errors.New("push endpoint is registered by another operator")
)

// Registration is what a browser registers.
type Registration struct {
	// Endpoint is the push service URL, the subscription's identity.
	Endpoint string
	// P256dh and Auth are the browser's public key and authentication secret, base64url.
	P256dh, Auth string
	UserAgent    *string
}

// Subscription is a registration as its operator sees it, without its keys.
type Subscription struct {
	Endpoint  string
	UserAgent *string
	CreatedAt time.Time
}

// Subscriptions are the browsers operators registered; each belongs to the operator who registered it.
type Subscriptions struct {
	db *sql.DB
}

func NewSubscriptions(db *sql.DB) *Subscriptions { return &Subscriptions{db: db} }

// transaction runs f in one transaction, which it commits when f succeeds.
func transaction(ctx context.Context, db *sql.DB, f func(*sql.Tx) error) error {
	return postgres.Transaction(ctx, db, sql.TxOptions{}, f)
}

func notify(ctx context.Context, tx *sql.Tx) error {
	return postgres.Notify(ctx, tx, UpdatesChannel, "")
}

// Save registers the browser for the operator. Registering an endpoint the operator holds again replaces its keys and
// user agent; one another operator holds is ErrRegisteredElsewhere.
func (s *Subscriptions) Save(ctx context.Context, operator store.OperatorPrincipal, registration Registration) error {
	return transaction(ctx, s.db, func(tx *sql.Tx) error {
		saved, err := tx.ExecContext(ctx, `
			INSERT INTO action_push_subscription
				(endpoint, p256dh, auth, user_agent, created_at, operator_issuer, operator_subject)
			VALUES ($1, $2, $3, $4, $5, $6, $7)
			ON CONFLICT (endpoint) DO UPDATE
			SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, user_agent = EXCLUDED.user_agent
			WHERE action_push_subscription.operator_issuer = EXCLUDED.operator_issuer
				AND action_push_subscription.operator_subject = EXCLUDED.operator_subject`,
			registration.Endpoint, registration.P256dh, registration.Auth, registration.UserAgent,
			time.Now().UTC().Truncate(time.Microsecond), operator.Issuer, operator.Subject)
		if err != nil {
			return err
		}
		if count, err := saved.RowsAffected(); err != nil {
			return err
		} else if count == 0 {
			return ErrRegisteredElsewhere
		}
		return notify(ctx, tx)
	})
}

// List returns the operator's subscriptions, oldest first.
func (s *Subscriptions) List(ctx context.Context, operator store.OperatorPrincipal) ([]Subscription, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT endpoint, user_agent, created_at FROM action_push_subscription
		WHERE operator_issuer = $1 AND operator_subject = $2 ORDER BY created_at, endpoint`,
		operator.Issuer, operator.Subject)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var subscriptions []Subscription
	for rows.Next() {
		var subscription Subscription
		if err := rows.Scan(&subscription.Endpoint, &subscription.UserAgent, &subscription.CreatedAt); err != nil {
			return nil, err
		}
		subscription.CreatedAt = subscription.CreatedAt.UTC()
		subscriptions = append(subscriptions, subscription)
	}
	return subscriptions, rows.Err()
}

// Delete removes the operator's subscription of the endpoint, with its ledger; one the operator does not hold is
// ErrNotFound.
func (s *Subscriptions) Delete(ctx context.Context, operator store.OperatorPrincipal, endpoint string) error {
	return transaction(ctx, s.db, func(tx *sql.Tx) error {
		deleted, err := tx.ExecContext(ctx, `
			DELETE FROM action_push_subscription
			WHERE endpoint = $1 AND operator_issuer = $2 AND operator_subject = $3`,
			endpoint, operator.Issuer, operator.Subject)
		if err != nil {
			return err
		}
		if count, err := deleted.RowsAffected(); err != nil {
			return err
		} else if count == 0 {
			return ErrNotFound
		}
		return notify(ctx, tx)
	})
}
