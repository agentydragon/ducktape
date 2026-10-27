package connections

import (
	"context"
	"database/sql"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		migrations.Up,
		func(database *postgres.Database) int {
			integrationDatabase = database
			return m.Run()
		},
	))
}

const (
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second
	testIssuer   = "https://test-issuer.example"
	// The operators a test calls as: the bearer `Bearer <subject>` proves the subject, of testIssuer.
	testOperator      = "test-operator"
	testOtherOperator = "test-other-operator"
	// updatesConnection is the application name of the LISTEN connection that wakes the watches.
	updatesConnection = "test-connection-updates"
)

var (
	callerAccount    = auth.Account{Namespace: "test-agentplane", Name: "test-caller"}
	otherAccount     = auth.Account{Namespace: "test-agentplane", Name: "test-other-caller"}
	unlabeledAccount = auth.Account{Namespace: "test-agentplane", Name: "test-unlabeled"}
)

// testGate stands in for the Action Service's Gate, which admits only operators: it takes every call's bearer
// `Bearer <subject>` as proof of that operator of testIssuer, until the test refuses the subject.
type testGate struct {
	mu       sync.Mutex
	refused  map[string]bool
	draining chan struct{}
}

func subjectOf(ctx context.Context) string {
	md, _ := metadata.FromIncomingContext(ctx)
	subject, _ := auth.BearerToken(md.Get("authorization"))
	return subject
}

func (g *testGate) Operator(ctx context.Context) (string, string) { return testIssuer, subjectOf(ctx) }

func (g *testGate) Recheck(ctx context.Context) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.refused[subjectOf(ctx)] {
		return status.Error(codes.Unauthenticated, "invalid bearer")
	}
	return nil
}

func (g *testGate) Draining() <-chan struct{} { return g.draining }

// refuse stops accepting the operator's bearer, as the Gate does once a token expires.
func (g *testGate) refuse(subject string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.refused[subject] = true
}

// testCallers is a view of the labeled ServiceAccounts that the test sets.
type testCallers struct {
	mu       sync.Mutex
	accounts []auth.Account
	stale    bool
}

func (c *testCallers) CallerServiceAccounts() ([]auth.Account, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stale {
		return nil, false
	}
	return slices.Clone(c.accounts), true
}

func (c *testCallers) goStale() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stale = true
}

// label makes the accounts the labeled ones, as the view sees labels added or removed.
func (c *testCallers) label(accounts ...auth.Account) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.accounts = accounts
}

// served is the service over a database of its own, on a gRPC server with its HTTP facade.
type served struct {
	db      *sql.DB
	store   *Store
	gate    *testGate
	callers *testCallers
	client  pb.ConnectionServiceClient
	// url is the base URL of the HTTP facade.
	url string
}

func serve(t *testing.T) *served {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	// The LISTEN connection is named so a test can drop it.
	listening, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	query := listening.Query()
	query.Set("application_name", updatesConnection)
	listening.RawQuery = query.Encode()
	updates, err := wakeups.Listen(t.Context(), listening.String(), UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { updates.Close() })
	s := &served{
		db:      db,
		gate:    &testGate{refused: map[string]bool{}, draining: make(chan struct{})},
		callers: &testCallers{accounts: []auth.Account{callerAccount, otherAccount}},
	}
	s.store = NewStore(db, s.callers)
	server := grpc.NewServer()
	pb.RegisterConnectionServiceServer(server, NewService(s.store, s.gate, updates))
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpFacade, err := facade.NewHandler(t.Context(), listener.Addr().String(), pb.RegisterConnectionServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	httpServer := &http.Server{Handler: httpFacade, ReadHeaderTimeout: callDeadline}
	facade.MountGRPC(httpServer, server)
	go func() { _ = httpServer.Serve(listener) }()
	t.Cleanup(func() { httpServer.Close() })
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	s.client = pb.NewConnectionServiceClient(connection)
	s.url = "http://" + listener.Addr().String()
	return s
}

// as is a call context that presents the operator's bearer, bounded by callDeadline.
func as(t *testing.T, subject string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+subject)
}

func wantCode(t *testing.T, what string, err error, code codes.Code) {
	t.Helper()
	if status.Code(err) != code {
		t.Errorf("%s: %v, want %v", what, err, code)
	}
}

// storedGrant is a grant as the authorization server stores it.
type storedGrant struct {
	caller auth.Account
	status grantStatus
}

// accountJSON is a ServiceAccount as the Python service writes it to a JSONB column.
func accountJSON(account auth.Account) string {
	return fmt.Sprintf(`{"namespace": %q, "name": %q}`, account.Namespace, account.Name)
}

// connect stores a Connection created at the time, as the authorization server writes one: bound to the account (nil
// leaves it unbound), at version 1, with the grants in revision order. An active grant was activated, and a revoked
// one revoked, a minute after the Connection was created.
func (s *served) connect(t *testing.T, name string, createdAt time.Time, bound *auth.Account, grants ...storedGrant) uuid.UUID {
	t.Helper()
	id := uuid.New()
	var boundCaller *string
	if bound != nil {
		document := accountJSON(*bound)
		boundCaller = &document
	}
	if _, err := s.db.ExecContext(t.Context(), `
		INSERT INTO external_connection (id, display_name, bound_caller, binding_version, version, created_at, updated_at)
		VALUES ($1, $2, $3::jsonb, 0, 1, $4, $4)`, id, name, boundCaller, createdAt); err != nil {
		t.Fatal(err)
	}
	later := createdAt.Add(time.Minute)
	for i, g := range grants {
		var activatedAt, revokedAt *time.Time
		switch g.status {
		case grantActive:
			activatedAt = &later
		case grantRevoked:
			revokedAt = &later
		}
		if _, err := s.db.ExecContext(t.Context(), `
			INSERT INTO external_connection_grant (id, connection_id, revision, original_caller, issuer, client_id,
				status, created_at, activated_at, revoked_at)
			VALUES ($1, $2, $3, $4::jsonb, 'https://test-actions.example', $5, $6, $7, $8, $9)`,
			uuid.New(), id, i+1, accountJSON(g.caller), fmt.Sprintf("test-client-%d", i+1),
			g.status, createdAt, activatedAt, revokedAt); err != nil {
			t.Fatal(err)
		}
	}
	return id
}

// active connects a Connection bound to the account through one active grant, created now.
func (s *served) active(t *testing.T, name string, account auth.Account) *pb.Connection {
	t.Helper()
	return s.get(t, s.connect(t, name, now(), &account, storedGrant{caller: account, status: grantActive}))
}

func (s *served) get(t *testing.T, id uuid.UUID) *pb.Connection {
	t.Helper()
	got, err := s.client.GetConnection(as(t, testOperator), &pb.GetConnectionRequest{ConnectionId: id.String()})
	if err != nil {
		t.Fatal(err)
	}
	return got.GetConnection()
}
