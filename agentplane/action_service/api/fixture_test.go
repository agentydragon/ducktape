package api

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"maps"
	"net"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/authtest"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	testAudience  = "test-actions"
	testNamespace = "test-agentplane"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second

	callerToken    = "test-token-caller"
	otherToken     = "test-token-other-caller"
	readerToken    = "test-token-reader"
	unlabeledToken = "test-token-unlabeled"
	outsiderToken  = "test-token-outsider"
)

// Bearer -> ServiceAccount. Test literals, not real credentials.
var testAccounts = map[string]authtest.Account{
	callerToken:    {Namespace: testNamespace, Name: "test-caller", UID: "test-caller-uid"},
	otherToken:     {Namespace: testNamespace, Name: "test-other-caller", UID: "test-other-caller-uid"},
	readerToken:    {Namespace: testNamespace, Name: "test-reader", UID: "test-reader-uid"},
	unlabeledToken: {Namespace: testNamespace, Name: "test-unlabeled", UID: "test-unlabeled-uid"},
	outsiderToken:  {Namespace: "test-elsewhere", Name: "test-caller", UID: "test-outsider-uid"},
}

var (
	caller      = store.CallerPrincipal{Account: account(callerToken)}
	otherCaller = store.CallerPrincipal{Account: account(otherToken)}
)

func account(token string) store.ServiceAccount {
	return store.ServiceAccount{Namespace: testAccounts[token].Namespace, Name: testAccounts[token].Name}
}

func workload(token string) auth.Account {
	return auth.Account{Namespace: testAccounts[token].Namespace, Name: testAccounts[token].Name}
}

// labels is the fake caller label: the accounts it holds carry it.
type labels struct {
	mu      sync.Mutex
	labeled map[auth.Account]bool
}

func (l *labels) Admits(account auth.Account) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.labeled[account]
}

func (l *labels) remove(account auth.Account) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.labeled, account)
}

// reviews is the fake TokenReview API: the accounts of authtest.Reviews, or a failure while unavailable is set.
type reviews struct {
	mu          sync.Mutex
	accounts    map[string]authtest.Account
	unavailable bool
}

func (r *reviews) Create(ctx context.Context, review *authenticationv1.TokenReview, options metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	r.mu.Lock()
	accounts, unavailable := maps.Clone(r.accounts), r.unavailable
	r.mu.Unlock()
	if unavailable {
		return nil, errors.New("test TokenReview API unavailable")
	}
	return authtest.Reviews{Accounts: accounts}.Create(ctx, review, options)
}

func (r *reviews) revoke(token string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.accounts, token)
}

func (r *reviews) setUnavailable() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.unavailable = true
}

// served is the API over a database of its own, on a gRPC server with its HTTP facade, as main serves it.
type served struct {
	store   *store.Store
	db      *sql.DB
	gate    *Gate
	issuer  *issuer
	labels  *labels
	reviews *reviews
	client  action_servicepb.ActionServiceClient
	// url is the base URL of the HTTP facade.
	url string
	// operatorToken is a token the issuer signed for the operator.
	operatorToken string
}

func serve(t *testing.T) *served {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	s := &served{
		store: store.New(db), db: db, issuer: newIssuer(t),
		labels: &labels{labeled: map[auth.Account]bool{
			workload(callerToken): true, workload(otherToken): true, workload(outsiderToken): true,
		}},
		reviews: &reviews{accounts: maps.Clone(testAccounts)},
	}
	s.operatorToken = s.issuer.token(t, nil)
	// Every call asks the TokenReview API again, so a revoked token is refused at once.
	workloads, err := auth.NewResolver(s.reviews, testAudience, []string{testNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	operators, err := NewOperatorVerifier(s.issuer.settings(ProfileAuthentik))
	if err != nil {
		t.Fatal(err)
	}
	s.gate = NewGate(GateOptions{
		Workloads: workloads, Callers: s.labels, ReaderAccounts: []auth.Account{workload(readerToken)}, Operators: operators,
	})
	grpcServer := grpc.NewServer(grpc.ChainUnaryInterceptor(s.gate.UnaryInterceptor), grpc.ChainStreamInterceptor(s.gate.StreamInterceptor))
	action_servicepb.RegisterActionServiceServer(grpcServer, NewService(s.store))
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpFacade, err := facade.NewHandler(t.Context(), listener.Addr().String(), action_servicepb.RegisterActionServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: httpFacade, ReadHeaderTimeout: callDeadline}
	facade.MountGRPC(server, grpcServer)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Close() })
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	s.client = action_servicepb.NewActionServiceClient(connection)
	s.url = "http://" + listener.Addr().String()
	return s
}

// as is a call context that presents the bearer, bounded by callDeadline.
func as(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

// submit stores a pending request of the owner, as admission would.
func (s *served) submit(t *testing.T, owner store.CallerPrincipal, key string) store.ActionRequest {
	t.Helper()
	request, err := s.store.Submit(t.Context(), owner, store.ActionRequestInput{
		IdempotencyKey: key, Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments: json.RawMessage(`{"test": "argument"}`), Title: "test title for " + key,
	}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	return request
}

// decide records the operator's Decision on a pending request.
func (s *served) decide(t *testing.T, request store.ActionRequest, verdict store.Verdict) store.ActionRequest {
	t.Helper()
	decided, _, err := s.store.Decide(t.Context(), request.ID, store.DecisionInput{
		Verdict: verdict, ExpectedVersion: request.Version, IdempotencyKey: "decision-" + request.IdempotencyKey,
	}, s.issuer.operator())
	if err != nil {
		t.Fatal(err)
	}
	return decided
}

func wantCode(t *testing.T, what string, err error, code codes.Code) {
	t.Helper()
	if status.Code(err) != code {
		t.Errorf("%s: %v, want %v", what, err, code)
	}
}
