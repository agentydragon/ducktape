package api

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/informer"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testAudience  = "test-actions"
	testNamespace = "test-agentplane"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second
	// callerLabel admits a ServiceAccount's workloads: sandbox_controller's ActionCallerLabel sets it, and Python's
	// CALLER_LABEL selects it.
	callerLabel = "agentplane.allegedly.works/use-action-service"
)

var (
	callerAccount    = auth.Account{Namespace: testNamespace, Name: "test-caller"}
	otherAccount     = auth.Account{Namespace: testNamespace, Name: "test-other-caller"}
	readerAccount    = auth.Account{Namespace: testNamespace, Name: "test-reader"}
	unlabeledAccount = auth.Account{Namespace: testNamespace, Name: "test-unlabeled"}
	// labeledReaderAccount is a service reader whose ServiceAccount also carries the caller label.
	labeledReaderAccount = auth.Account{Namespace: testNamespace, Name: "test-labeled-reader"}
	// outsiderAccount is labeled, outside the namespaces whose workloads may call.
	outsiderAccount = auth.Account{Namespace: "test-elsewhere", Name: "test-caller"}

	caller      = principal(callerAccount)
	otherCaller = principal(otherAccount)

	kube *kubeapiserver.Server
	// policyObjects writes PolicySets and PolicyBindings to the API server.
	policyObjects client.Client
	// Each proves its account, from a Pod running as it.
	callerToken, otherToken, readerToken, labeledReaderToken, unlabeledToken, outsiderToken string
)

func principal(account auth.Account) store.CallerPrincipal {
	return store.CallerPrincipal{Account: store.ServiceAccount{Namespace: account.Namespace, Name: account.Name}}
}

func useAPIServer(server *kubeapiserver.Server) error {
	kube = server
	ctx := context.Background()
	for _, account := range []auth.Account{callerAccount, otherAccount, labeledReaderAccount, outsiderAccount} {
		if err := setCallerLabel(ctx, account, true); err != nil {
			return err
		}
	}
	for token, account := range map[*string]auth.Account{
		&callerToken: callerAccount, &otherToken: otherAccount, &readerToken: readerAccount,
		&labeledReaderToken: labeledReaderAccount, &unlabeledToken: unlabeledAccount, &outsiderToken: outsiderAccount,
	} {
		var err error
		if *token, err = server.WorkloadToken(ctx, account, testAudience); err != nil {
			return err
		}
	}
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		return err
	}
	var err error
	policyObjects, err = client.New(server.Config, client.Options{Scheme: scheme})
	return err
}

// setCallerLabel sets or removes the caller label on the account's ServiceAccount, which it creates when absent.
func setCallerLabel(ctx context.Context, account auth.Account, labeled bool) error {
	if _, err := kube.ServiceAccount(ctx, account); err != nil {
		return err
	}
	value := "null"
	if labeled {
		value = `"true"`
	}
	_, err := kube.Clientset.CoreV1().ServiceAccounts(account.Namespace).Patch(ctx, account.Name, types.MergePatchType,
		fmt.Appendf(nil, `{"metadata":{"labels":{%q:%s}}}`, callerLabel, value), metav1.PatchOptions{})
	return err
}

// labeledAccounts reads the caller label from each ServiceAccount on the API server, where the policy informer's
// index will watch it.
type labeledAccounts struct{}

func (labeledAccounts) Admits(account auth.Account) bool {
	found, err := kube.Clientset.CoreV1().ServiceAccounts(account.Namespace).Get(context.Background(), account.Name, metav1.GetOptions{})
	return err == nil && found.Labels[callerLabel] == "true"
}

// served is the API over a database of its own, on a gRPC server with its HTTP facade, as main serves it.
type served struct {
	store  *store.Store
	db     *sql.DB
	gate   *Gate
	issuer *issuer
	client action_servicepb.ActionServiceClient
	// operators is the OperatorActionService client.
	operators operator_action_servicepb.OperatorActionServiceClient
	// url is the base URL of the HTTP facade.
	url string
	// operatorToken is a token the issuer signed for the operator.
	operatorToken string
}

// updatesConnection is the application name of the LISTEN connection that wakes the watches.
const updatesConnection = "test-action-updates"

func serve(t *testing.T) *served {
	t.Helper()
	return serveWith(t, kube.TokenReviews(), unwatchedPolicy())
}

// unwatchedPolicy is a policy index no informer fills, which is never available.
func unwatchedPolicy() *informer.Index { return informer.NewIndex(time.Hour, nil) }

// serveWith serves the API with workload tokens reviewed by reviews, and Action policy read from the policy index.
func serveWith(t *testing.T, reviews auth.TokenReviews, policy *informer.Index) *served {
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
	updates, err := wakeups.Listen(t.Context(), listening.String(), store.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { updates.Close() })
	s := &served{store: store.New(db), db: db, issuer: newIssuer(t)}
	s.operatorToken = s.issuer.token(t, nil)
	// Every call asks the TokenReview API again, so a revoked token is refused once the API server refuses it.
	workloads, err := auth.NewResolver(reviews, testAudience, []string{testNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	operators, err := NewOperatorVerifier(s.issuer.settings(ProfileAuthentik))
	if err != nil {
		t.Fatal(err)
	}
	s.gate = NewGate(GateOptions{
		Workloads: workloads, Callers: labeledAccounts{}, ReaderAccounts: []auth.Account{readerAccount, labeledReaderAccount},
		Operators: operators,
	})
	grpcServer := grpc.NewServer(grpc.ChainUnaryInterceptor(s.gate.UnaryInterceptor), grpc.ChainStreamInterceptor(s.gate.StreamInterceptor))
	action_servicepb.RegisterActionServiceServer(grpcServer, NewService(s.store, updates, s.gate, policy))
	operator_action_servicepb.RegisterOperatorActionServiceServer(grpcServer, NewOperatorService(s.store))
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpFacade, err := facade.NewHandler(
		t.Context(), listener.Addr().String(),
		action_servicepb.RegisterActionServiceHandlerFromEndpoint,
		operator_action_servicepb.RegisterOperatorActionServiceHandlerFromEndpoint,
	)
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
	s.operators = operator_action_servicepb.NewOperatorActionServiceClient(connection)
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

// get is the HTTP facade's answer to a GET of path with the bearer, or with none when token is empty.
func (s *served) get(t *testing.T, path, token string) (int, []byte) {
	t.Helper()
	call, err := http.NewRequestWithContext(t.Context(), http.MethodGet, s.url+path, nil)
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		call.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := http.DefaultClient.Do(call)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, body
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
