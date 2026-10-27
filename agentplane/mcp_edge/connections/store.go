// Package connections is the authority over external Connections: MCP clients outside the cluster that act on
// Actions as a ServiceAccount, each through a consented OAuth grant, and the operator consent (an enrollment) that
// precedes a grant. It owns the tables `external_connection`, `external_connection_grant` and
// `connection_enrollment`, and serves `agentplane.mcp_edge.ConnectionService` to operators.
//
// The Action Service's core neither imports it nor rechecks a grant: a request a Connection makes is its
// ServiceAccount's, so unbinding or rebinding a Connection changes only what its tokens do next.
package connections

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/go"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// UpdatesChannel is notified, with no payload, by every commit that changes a Connection or one of its grants. The
// rows stay the authority: a woken reader reads them again.
const UpdatesChannel = "agentplane_connection_updates"

// Callers is the view of the ServiceAccounts labeled as Action callers: the accounts the Action Service admits, and so
// the only ones a Connection or a consent may act as.
type Callers interface {
	// CallerServiceAccounts are the labeled accounts in namespace and name order, and whether the view is current:
	// none when it is not.
	CallerServiceAccounts() ([]auth.Account, bool)
}

// Store reads and writes the Connections, their grants and the enrollments in PostgreSQL. Every write is one
// transaction; one that changes a Connection or a grant notifies UpdatesChannel in it.
type Store struct {
	db      *sql.DB
	callers Callers
}

func NewStore(db *sql.DB, callers Callers) *Store { return &Store{db: db, callers: callers} }

// labeled is whether the account is a caller in a current view of the callers.
func (s *Store) labeled(account auth.Account) bool {
	accounts, current := s.callers.CallerServiceAccounts()
	return current && slices.Contains(accounts, account)
}

type grantStatus string

const (
	grantPending grantStatus = "pending"
	grantActive  grantStatus = "active"
	grantRevoked grantStatus = "revoked"
)

// grant is one consented OAuth authorization of a Connection. Only its status changes.
type grant struct {
	id, connectionID uuid.UUID
	revision         int32
	// caller is the ServiceAccount consent chose; a rebind changes the Connection's boundCaller, not this.
	caller                 auth.Account
	issuer, clientID       string
	status                 grantStatus
	createdAt              time.Time
	activatedAt, revokedAt *time.Time
}

// connection is a Connection with every grant it has had, oldest revision first.
type connection struct {
	id                   uuid.UUID
	displayName          string
	version              int64
	createdAt, updatedAt time.Time
	grants               []grant
	// boundCaller is the account its unrevoked grant acts as; nil exactly when the Connection is unbound.
	boundCaller *auth.Account
}

// position is a Connection's place in the oldest-first order.
type position struct {
	createdAt time.Time
	id        uuid.UUID
}

func (c connection) position() position { return position{createdAt: c.createdAt, id: c.id} }

// errStored is a stored row no message can show: a fault of the service, not of the caller.
var errStored = errors.New("stored Connection cannot be shown")

var (
	errConnectionNotFound = status.Error(codes.NotFound, "Connection not found")
	errVersionChanged     = status.Error(codes.Aborted, "Connection version changed")
)

// notLabeled refuses an account a Connection or a consent may not act as.
const notLabeled = "the ServiceAccount is missing or not labeled as an Action caller"

// now is the time a write records: PostgreSQL keeps microseconds, so a value read back equals the one written.
func now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

func (s *Store) transaction(ctx context.Context, f func(*sql.Tx) error) error {
	return postgres.Transaction(ctx, s.db, sql.TxOptions{}, f)
}

// read runs f on one snapshot, so a Connection and its grants agree.
func (s *Store) read(ctx context.Context, f func(*sql.Tx) error) error {
	return postgres.ReadTransaction(ctx, s.db, sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true}, f)
}

// accountDocument is a ServiceAccount as the JSONB columns hold it.
type accountDocument struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

func encodeAccount(account auth.Account) ([]byte, error) {
	return json.Marshal(accountDocument(account))
}

func decodeAccount(document []byte) (auth.Account, error) {
	var account accountDocument
	if err := json.Unmarshal(document, &account); err != nil || account.Namespace == "" || account.Name == "" {
		return auth.Account{}, fmt.Errorf("%w: ServiceAccount %s", errStored, document)
	}
	return auth.Account(account), nil
}

const connectionColumns = `id, display_name, version, created_at, updated_at, bound_caller`

func scanConnections(ctx context.Context, tx *sql.Tx, query string, args ...any) ([]connection, error) {
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var connections []connection
	for rows.Next() {
		var c connection
		var bound []byte
		if err := rows.Scan(&c.id, &c.displayName, &c.version, &c.createdAt, &c.updatedAt, &bound); err != nil {
			return nil, err
		}
		if bound != nil {
			account, err := decodeAccount(bound)
			if err != nil {
				return nil, err
			}
			c.boundCaller = &account
		}
		connections = append(connections, c)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return connections, attachGrants(ctx, tx, connections)
}

// attachGrants reads the grants of the connections into them.
func attachGrants(ctx context.Context, tx *sql.Tx, connections []connection) error {
	if len(connections) == 0 {
		return nil
	}
	ids := make(pq.StringArray, len(connections))
	index := make(map[uuid.UUID]int, len(connections))
	for i, c := range connections {
		ids[i] = c.id.String()
		index[c.id] = i
	}
	rows, err := tx.QueryContext(ctx, `
		SELECT id, connection_id, revision, original_caller, issuer, client_id, status, created_at,
			activated_at, revoked_at
		FROM external_connection_grant WHERE connection_id = ANY($1::uuid[]) ORDER BY revision`, ids)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var g grant
		var caller []byte
		if err := rows.Scan(
			&g.id, &g.connectionID, &g.revision, &caller, &g.issuer, &g.clientID, &g.status, &g.createdAt,
			&g.activatedAt, &g.revokedAt,
		); err != nil {
			return err
		}
		if g.caller, err = decodeAccount(caller); err != nil {
			return err
		}
		owner := &connections[index[g.connectionID]]
		owner.grants = append(owner.grants, g)
	}
	return rows.Err()
}

// list returns the Connections oldest first: those after `after` when it is set, and at most limit of them when
// limit is positive.
func (s *Store) list(ctx context.Context, after *position, limit int) ([]connection, error) {
	query, args := `SELECT `+connectionColumns+` FROM external_connection`, []any{}
	if after != nil {
		query += ` WHERE (created_at, id) > ($1, $2)`
		args = append(args, after.createdAt, after.id)
	}
	query += ` ORDER BY created_at, id`
	if limit > 0 {
		query += fmt.Sprintf(` LIMIT %d`, limit)
	}
	var connections []connection
	err := s.read(ctx, func(tx *sql.Tx) error {
		var err error
		connections, err = scanConnections(ctx, tx, query, args...)
		return err
	})
	return connections, err
}

func (s *Store) get(ctx context.Context, id uuid.UUID) (connection, error) {
	var found []connection
	err := s.read(ctx, func(tx *sql.Tx) error {
		var err error
		found, err = scanConnections(ctx, tx, `SELECT `+connectionColumns+` FROM external_connection WHERE id = $1`, id)
		return err
	})
	if err != nil {
		return connection{}, err
	}
	if len(found) == 0 {
		return connection{}, errConnectionNotFound
	}
	return found[0], nil
}

// versions is the version of every Connection.
func (s *Store) versions(ctx context.Context) (map[uuid.UUID]int64, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id, version FROM external_connection`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	versions := map[uuid.UUID]int64{}
	for rows.Next() {
		var id uuid.UUID
		var version int64
		if err := rows.Scan(&id, &version); err != nil {
			return nil, err
		}
		versions[id] = version
	}
	return versions, rows.Err()
}

// find returns the Connections among ids, in no particular order.
func (s *Store) find(ctx context.Context, ids []uuid.UUID) ([]connection, error) {
	values := make(pq.StringArray, len(ids))
	for i, id := range ids {
		values[i] = id.String()
	}
	var connections []connection
	err := s.read(ctx, func(tx *sql.Tx) error {
		var err error
		connections, err = scanConnections(ctx, tx,
			`SELECT `+connectionColumns+` FROM external_connection WHERE id = ANY($1::uuid[])`, values)
		return err
	})
	return connections, err
}

// change runs changeConnection in a transaction of its own.
func (s *Store) change(ctx context.Context, id uuid.UUID, f func(*sql.Tx, *connection) (bool, error)) (connection, error) {
	var changed connection
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var err error
		changed, err = changeConnection(ctx, tx, id, f)
		return err
	})
	return changed, err
}

// changeConnection locks the Connection, lets f change it, and writes it back and notifies UpdatesChannel when f
// reports that it did. A Connection's grants are changed only under its lock.
func changeConnection(
	ctx context.Context, tx *sql.Tx, id uuid.UUID, f func(*sql.Tx, *connection) (bool, error),
) (connection, error) {
	found, err := scanConnections(ctx, tx, `SELECT `+connectionColumns+` FROM external_connection WHERE id = $1 FOR UPDATE`, id)
	if err != nil {
		return connection{}, err
	}
	if len(found) == 0 {
		return connection{}, errConnectionNotFound
	}
	changed := found[0]
	wrote, err := f(tx, &changed)
	if err != nil || !wrote {
		return changed, err
	}
	changed.version++
	changed.updatedAt = now()
	// NULL while the Connection is unbound.
	var bound any
	if changed.boundCaller != nil {
		document, err := encodeAccount(*changed.boundCaller)
		if err != nil {
			return changed, err
		}
		bound = document
	}
	if _, err := tx.ExecContext(ctx, `
		UPDATE external_connection SET display_name = $2, bound_caller = $3::jsonb, version = $4, updated_at = $5
		WHERE id = $1`, changed.id, changed.displayName, bound, changed.version, changed.updatedAt); err != nil {
		return changed, err
	}
	return changed, notify(ctx, tx)
}

// notify wakes the readers of UpdatesChannel when tx commits.
func notify(ctx context.Context, tx *sql.Tx) error {
	return postgres.Notify(ctx, tx, UpdatesChannel, "")
}

func requireVersion(c *connection, expected int64) error {
	if c.version != expected {
		return errVersionChanged
	}
	return nil
}

func (s *Store) rename(ctx context.Context, id uuid.UUID, expectedVersion int64, name string) (connection, error) {
	return s.change(ctx, id, func(_ *sql.Tx, c *connection) (bool, error) {
		if err := requireVersion(c, expectedVersion); err != nil {
			return false, err
		}
		c.displayName = name
		return true, nil
	})
}

// rebind retargets the Connection's active grant to another labeled account. The grants keep the account consent
// chose.
func (s *Store) rebind(ctx context.Context, id uuid.UUID, expectedVersion int64, account auth.Account) (connection, error) {
	return s.change(ctx, id, func(_ *sql.Tx, c *connection) (bool, error) {
		if err := requireVersion(c, expectedVersion); err != nil {
			return false, err
		}
		active := slices.ContainsFunc(c.grants, func(g grant) bool { return g.status == grantActive })
		switch {
		case !active:
			return false, status.Error(codes.FailedPrecondition, "the Connection has no active grant")
		case c.boundCaller == nil:
			return false, status.Error(codes.FailedPrecondition, "the Connection is unbound")
		case !s.labeled(account):
			return false, status.Error(codes.FailedPrecondition, notLabeled)
		case *c.boundCaller == account:
			return false, nil
		}
		c.boundCaller = &account
		return true, nil
	})
}

// unbind revokes every grant of the Connection and clears its bound caller. A Connection whose grants are all
// revoked already is answered as it is, whatever version was expected.
func (s *Store) unbind(ctx context.Context, id uuid.UUID, expectedVersion int64) (connection, error) {
	return s.change(ctx, id, func(tx *sql.Tx, c *connection) (bool, error) {
		if !slices.ContainsFunc(c.grants, func(g grant) bool { return g.status != grantRevoked }) {
			return false, nil
		}
		if err := requireVersion(c, expectedVersion); err != nil {
			return false, err
		}
		revokedAt := now()
		for i := range c.grants {
			if c.grants[i].status != grantRevoked {
				c.grants[i].status, c.grants[i].revokedAt = grantRevoked, &revokedAt
			}
		}
		c.boundCaller = nil
		_, err := tx.ExecContext(ctx, `
			UPDATE external_connection_grant SET status = $2, revoked_at = $3 WHERE connection_id = $1 AND status <> $2`,
			c.id, grantRevoked, revokedAt)
		return true, err
	})
}
