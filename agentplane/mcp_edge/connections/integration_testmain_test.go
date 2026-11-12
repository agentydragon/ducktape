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

	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/migrations"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
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
	callerIdentity      = auth.Identity{Name: "test-caller", UID: "uid-test-caller"}
	otherIdentity       = auth.Identity{Name: "test-other-caller", UID: "uid-test-other-caller"}
	unavailableIdentity = auth.Identity{Name: "test-unavailable", UID: "uid-test-unavailable"}
)

// testGate stands in for the Action Service's Gate, which admits only operators: it takes every call's bearer
// `Bearer <subject>` as proof of that operator of testIssuer, until the test refuses the subject.
type testGate struct {
	mu       sync.Mutex
	refused  map[string]bool
	draining context.Context
	drain    context.CancelFunc
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

func (g *testGate) DrainingContext() context.Context { return g.draining }

// refuse stops accepting the operator's bearer, as the Gate does once a token expires.
func (g *testGate) refuse(subject string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.refused[subject] = true
}

// testIdentities is the current policy namespace Identity view and direct UID reader the test sets.
type testIdentities struct {
	mu         sync.Mutex
	identities []auth.Identity
	uids       map[string]string
	stale      bool
}

func (c *testIdentities) ConnectionIdentities() ([]auth.Identity, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stale {
		return nil, false
	}
	return slices.Clone(c.identities), true
}

func (c *testIdentities) GetIdentityUID(_ context.Context, name string) (string, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	uid, ok := c.uids[name]
	return uid, ok, nil
}

func (c *testIdentities) goStale() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stale = true
}

// live makes the named Identities the picker can currently offer.
func (c *testIdentities) live(identities ...auth.Identity) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.identities = identities
	c.uids = make(map[string]string, len(identities))
	for _, identity := range identities {
		c.uids[identity.Name] = identity.UID
	}
}

// served is the service over a database of its own, on a gRPC server with its HTTP facade.
type served struct {
	db         *sql.DB
	store      *Store
	gate       *testGate
	identities *testIdentities
	client     pb.ConnectionServiceClient
	// url is the base URL of the HTTP facade.
	url string
}

func serve(t *testing.T) *served {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db := postgres.Open(t, dsn)
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
	draining, drain := context.WithCancel(context.Background())
	t.Cleanup(drain)
	s := &served{
		db:   db,
		gate: &testGate{refused: map[string]bool{}, draining: draining, drain: drain},
		identities: &testIdentities{
			identities: []auth.Identity{callerIdentity, otherIdentity},
			uids:       map[string]string{callerIdentity.Name: callerIdentity.UID, otherIdentity.Name: otherIdentity.UID},
		},
	}
	s.store = NewStore(db, s.identities, s.identities)
	server := grpc.NewServer()
	pb.RegisterConnectionServiceServer(server, NewService(s.store, s.gate, updates))
	grpcListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpFacade, err := facade.NewHandler(t.Context(), grpcListener.Addr().String(), 0, pb.RegisterConnectionServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	httpServer := &http.Server{Handler: httpFacade, ReadHeaderTimeout: callDeadline}
	go func() { _ = server.Serve(grpcListener) }()
	t.Cleanup(server.Stop)
	go func() { _ = httpServer.Serve(httpListener) }()
	t.Cleanup(func() { httpServer.Close() })
	connection, err := grpc.NewClient(grpcListener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	s.client = pb.NewConnectionServiceClient(connection)
	s.url = "http://" + httpListener.Addr().String()
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

// storedGrant is a grant as the authorization server stores it. caller is historical legacy attribution.
type storedGrant struct {
	caller   auth.Account
	identity *auth.Identity
	status   grantStatus
}

// accountJSON is a ServiceAccount as the Python service writes it to a JSONB column.
func accountJSON(account auth.Account) string {
	return fmt.Sprintf(`{"namespace": %q, "name": %q}`, account.Namespace, account.Name)
}

func identityJSON(identity auth.Identity) string {
	return fmt.Sprintf(`{"name": %q, "uid": %q}`, identity.Name, identity.UID)
}

// connect stores a Connection created at the time, as the authorization server writes one: bound to the account (nil
// leaves it unbound), at version 1, with the grants in revision order. An active grant was activated, and a revoked
// one revoked, a minute after the Connection was created.
func (s *served) connect(t *testing.T, name string, createdAt time.Time, bound *auth.Identity, grants ...storedGrant) uuid.UUID {
	t.Helper()
	id := uuid.New()
	var boundIdentity *string
	if bound != nil {
		document := identityJSON(*bound)
		boundIdentity = &document
	}
	if _, err := s.db.ExecContext(t.Context(), `
		INSERT INTO external_connection (id, display_name, bound_identity, identity_binding_required, binding_version, version, created_at, updated_at)
		VALUES ($1, $2, $3::jsonb, false, 0, 1, $4, $4)`, id, name, boundIdentity, createdAt); err != nil {
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
		var callerDocument, identityDocument *string
		if g.caller.Name != "" {
			document := accountJSON(g.caller)
			callerDocument = &document
		}
		if g.identity != nil {
			document := identityJSON(*g.identity)
			identityDocument = &document
		}
		if _, err := s.db.ExecContext(t.Context(), `
			INSERT INTO external_connection_grant (id, connection_id, revision, original_caller, original_identity, issuer, client_id,
				status, created_at, activated_at, revoked_at)
			VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, 'https://test-actions.example', $6, $7, $8, $9, $10)`,
			uuid.New(), id, i+1, callerDocument, identityDocument, fmt.Sprintf("test-client-%d", i+1),
			g.status, createdAt, activatedAt, revokedAt); err != nil {
			t.Fatal(err)
		}
	}
	return id
}

// active connects a Connection bound to the account through one active grant, created now.
func (s *served) active(t *testing.T, name string, identity auth.Identity) *pb.Connection {
	t.Helper()
	return s.get(t, s.connect(t, name, now(), &identity, storedGrant{identity: &identity, status: grantActive}))
}

func (s *served) get(t *testing.T, id uuid.UUID) *pb.Connection {
	t.Helper()
	got, err := s.client.GetConnection(as(t, testOperator), &pb.GetConnectionRequest{ConnectionId: id.String()})
	if err != nil {
		t.Fatal(err)
	}
	return got.GetConnection()
}
