package connections

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// An enrollment is the consent of one OAuth authorization in flight. The authorization server creates it and later
// turns an allowed one into a grant; between the two, the operator who previews it first decides it. Only a hash of
// the handle in the consent URL is stored.

// Operator is a person who decides enrollments: the issuer of their token and its subject.
type Operator struct{ Issuer, Subject string }

type verdict string

const (
	verdictAllow verdict = "allow"
	verdictDeny  verdict = "deny"
)

// enrollment is a consent as an operator previews and decides it.
type enrollment struct {
	id uuid.UUID
	// issuer and clientID are the authorization server's and its client's.
	issuer, clientID string
	clientName       *string
	// redirectURI is the OAuth client's; upstreamURL is where an allowed consent sends the browser next.
	redirectURI, upstreamURL string
	expiresAt                time.Time
	version                  int64
	// operator is the first preview's; nil until then.
	operator *Operator
	// verdict is nil until the enrollment is decided, and allowed is nil unless it was allowed.
	verdict *verdict
	allowed *allowance
	// claimedAt is when a code exchange turned the allowed enrollment into a grant; nil until then.
	claimedAt *time.Time
}

// decision is an operator's decision on an enrollment.
type decision struct {
	expectedVersion int64
	// allow is nil for a refusal.
	allow *allowance
}

// allowance is a consent: the labeled account the Connection acts as, and the Connection.
type allowance struct {
	account    auth.Account
	connection connectionChoice
}

// connectionChoice is the Connection a consent creates or replaces: newConnection or reconnection.
type connectionChoice interface{ choice() }

type newConnection struct{ displayName string }

// reconnection replaces the authority of an existing Connection, as the operator reviewed it at expectedVersion.
type reconnection struct {
	id              uuid.UUID
	expectedVersion int64
}

func (newConnection) choice() {}
func (reconnection) choice()  {}

// decisionResult is the recorded decision: where an allowed one sends the browser next.
type decisionResult struct {
	verdict     verdict
	redirectURL *string
}

var (
	errEnrollmentNotFound = status.Error(codes.NotFound, "enrollment not found")
	errEnrollmentExpired  = status.Error(codes.FailedPrecondition, "enrollment expired; restart authorization")
	errOtherOperator      = status.Error(codes.PermissionDenied, "enrollment is pinned to another operator")
)

func digest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// lockEnrollment reads the live enrollment the handle names and locks it.
func lockEnrollment(ctx context.Context, tx *sql.Tx, handle string) (enrollment, error) {
	e, err := lockEnrollmentWhere(ctx, tx, "handle_hash", digest(handle))
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return e, errEnrollmentNotFound
	case err != nil:
		return e, err
	case !e.expiresAt.After(time.Now()):
		return e, errEnrollmentExpired
	}
	return e, nil
}

// lockEnrollmentWhere reads the enrollment whose column holds the value, live or not, and locks it.
func lockEnrollmentWhere(ctx context.Context, tx *sql.Tx, column string, value any) (enrollment, error) {
	var e enrollment
	var issuer, subject, displayName *string
	var caller []byte
	var connectionID *uuid.UUID
	var connectionVersion *int64
	err := tx.QueryRowContext(
		ctx, `
		SELECT id, issuer, client_id, client_name, redirect_uri, upstream_url, expires_at, version, operator_issuer,
			operator_subject, verdict, caller, display_name, connection_id, connection_version, exchange_claimed_at
		FROM connection_enrollment WHERE `+column+` = $1 FOR UPDATE`, value,
	).Scan(
		&e.id, &e.issuer, &e.clientID, &e.clientName, &e.redirectURI, &e.upstreamURL, &e.expiresAt, &e.version,
		&issuer, &subject, &e.verdict, &caller, &displayName, &connectionID, &connectionVersion, &e.claimedAt,
	)
	if err != nil {
		return e, err
	}
	if issuer != nil && subject != nil {
		e.operator = &Operator{Issuer: *issuer, Subject: *subject}
	}
	if caller != nil {
		account, err := decodeAccount(caller)
		if err != nil {
			return e, err
		}
		e.allowed = &allowance{account: account}
		switch {
		case connectionID != nil && connectionVersion != nil:
			e.allowed.connection = reconnection{id: *connectionID, expectedVersion: *connectionVersion}
		case displayName != nil:
			e.allowed.connection = newConnection{displayName: *displayName}
		default:
			return e, fmt.Errorf("%w: enrollment %s allows no Connection", errStored, e.id)
		}
	}
	return e, nil
}

// requireOperator refuses all but the operator of the enrollment's first preview.
func (e enrollment) requireOperator(by Operator) error {
	if e.operator == nil || *e.operator != by {
		return errOtherOperator
	}
	return nil
}

// previewEnrollment shows the operator the enrollment, which the first preview pins to its operator.
func (s *Store) previewEnrollment(ctx context.Context, handle string, by Operator) (enrollment, error) {
	var previewed enrollment
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		e, err := lockEnrollment(ctx, tx, handle)
		if err != nil {
			return err
		}
		if e.operator == nil {
			if _, err := tx.ExecContext(ctx, `
				UPDATE connection_enrollment SET operator_issuer = $2, operator_subject = $3 WHERE id = $1`,
				e.id, by.Issuer, by.Subject); err != nil {
				return err
			}
			e.operator = &by
		}
		previewed = e
		return e.requireOperator(by)
	})
	return previewed, err
}

// decideEnrollment records the operator's decision once: any decision after it, even a repeat after a lost answer, is
// refused, and the operator restarts the authorization from the MCP client. An allow records the account and the
// Connection: it grants nothing until the authorization server turns it into a grant.
func (s *Store) decideEnrollment(ctx context.Context, handle string, d decision, by Operator) (decisionResult, error) {
	var result decisionResult
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		e, err := lockEnrollment(ctx, tx, handle)
		if err != nil {
			return err
		}
		if err := e.requireOperator(by); err != nil {
			return err
		}
		if e.verdict != nil {
			return status.Error(codes.FailedPrecondition, "enrollment already decided; restart authorization")
		}
		if e.version != d.expectedVersion {
			return status.Error(codes.Aborted, "enrollment version changed")
		}
		decided := verdictDeny
		var caller any
		var displayName *string
		var connectionID *uuid.UUID
		var connectionVersion *int64
		if d.allow != nil {
			if !s.labeled(d.allow.account) {
				return status.Error(codes.PermissionDenied, notLabeled)
			}
			decided = verdictAllow
			if caller, err = encodeAccount(d.allow.account); err != nil {
				return err
			}
			switch choice := d.allow.connection.(type) {
			case newConnection:
				displayName = &choice.displayName
			case reconnection:
				var current int64
				err := tx.QueryRowContext(ctx, `SELECT version FROM external_connection WHERE id = $1`, choice.id).Scan(&current)
				switch {
				case errors.Is(err, sql.ErrNoRows):
					return errConnectionNotFound
				case err != nil:
					return err
				case current != choice.expectedVersion:
					return status.Error(codes.Aborted, "the Connection changed since it was reviewed; restart authorization")
				}
				connectionID, connectionVersion = &choice.id, &choice.expectedVersion
			}
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE connection_enrollment SET verdict = $2, version = version + 1, caller = $3::jsonb, display_name = $4,
				connection_id = $5, connection_version = $6
			WHERE id = $1`, e.id, decided, caller, displayName, connectionID, connectionVersion); err != nil {
			return err
		}
		result = decisionResult{verdict: decided}
		if decided == verdictAllow {
			result.redirectURL = &e.upstreamURL
		}
		return nil
	})
	return result, err
}
