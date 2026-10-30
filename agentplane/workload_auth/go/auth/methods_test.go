package auth_test

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// callerHealth stands in for a served API: it answers SERVING and records the caller each call was admitted as.
type callerHealth struct {
	healthpb.UnimplementedHealthServer
	callers chan auth.Principal
}

func (h callerHealth) record(ctx context.Context) {
	principal, err := auth.Caller(ctx)
	if err == nil {
		h.callers <- principal
	}
}

func (h callerHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	h.record(ctx)
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

func (h callerHealth) Watch(_ *healthpb.HealthCheckRequest, stream grpc.ServerStreamingServer[healthpb.HealthCheckResponse]) error {
	h.record(stream.Context())
	return stream.Send(&healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING})
}

// serveHealth serves callerHealth behind methods and returns a client and the callers the calls were admitted as.
func serveHealth(t *testing.T, methods auth.Methods, admissionTimeout time.Duration) (healthpb.HealthClient, <-chan auth.Principal) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	callers := make(chan auth.Principal, 8)
	server := grpc.NewServer(methods.ServerOptions(admissionTimeout)...)
	healthpb.RegisterHealthServer(server, callerHealth{callers: callers})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return healthpb.NewHealthClient(connection), callers
}

func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	t.Cleanup(cancel)
	if token == "" {
		return ctx
	}
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

// checkAndWatch is the codes of a unary Check and of a Watch's first frame.
func checkAndWatch(ctx context.Context, client healthpb.HealthClient) (codes.Code, codes.Code) {
	_, checked := client.Check(ctx, &healthpb.HealthCheckRequest{})
	stream, err := client.Watch(ctx, &healthpb.HealthCheckRequest{})
	if err == nil {
		_, err = stream.Recv()
	}
	return status.Code(checked), status.Code(err)
}

func both(t *testing.T, ctx context.Context, client healthpb.HealthClient, want codes.Code) {
	t.Helper()
	if check, watch := checkAndWatch(ctx, client); check != want || watch != want {
		t.Errorf("Check %v, Watch %v; want %v", check, watch, want)
	}
}

func TestAMethodNoRuleListsIsRefusedBeforeItsBearerIsReviewed(t *testing.T) {
	_, token := workload(t, app)
	reviews := &kubeapiserver.CountedReviews{TokenReviews: api.TokenReviews()}
	resolver := newResolver(t, reviews)
	for name, methods := range map[string]auth.Methods{
		"unlisted":      {healthpb.Health_List_FullMethodName: auth.Accounts(resolver, []auth.Account{app})},
		"the zero Rule": {healthpb.Health_Check_FullMethodName: {}, healthpb.Health_Watch_FullMethodName: {}},
	} {
		t.Run(name, func(t *testing.T) {
			client, _ := serveHealth(t, methods, 0)
			both(t, bearing(t, token), client, codes.PermissionDenied)
		})
	}
	if calls := reviews.Calls.Load(); calls != 0 {
		t.Fatalf("%d reviews for methods no Rule admits to", calls)
	}
}

func TestAnAccountsRuleAdmitsOnlyItsAccountsAndHandsTheHandlerTheCaller(t *testing.T) {
	pod, token := workload(t, app)
	_, strangerToken := workload(t, auth.Account{Namespace: testNamespace, Name: "test-stranger"})
	resolver := newResolver(t, api.TokenReviews())
	rule := auth.Accounts(resolver, []auth.Account{app})
	client, callers := serveHealth(t, auth.Methods{
		healthpb.Health_Check_FullMethodName: rule, healthpb.Health_Watch_FullMethodName: rule,
	}, 0)
	both(t, bearing(t, ""), client, codes.Unauthenticated)
	both(t, bearing(t, "test-token-unknown"), client, codes.Unauthenticated)
	both(t, bearing(t, strangerToken), client, codes.PermissionDenied)
	both(t, bearing(t, token), client, codes.OK)
	for range 2 {
		if caller := <-callers; caller.Account != app || caller.PodUID != string(pod.UID) {
			t.Errorf("handler saw caller %+v, want %v in %s", caller, app, pod.Name)
		}
	}
}

func TestAnAccountsRuleWithNoAccountsAdmitsNobody(t *testing.T) {
	_, token := workload(t, app)
	rule := auth.Accounts(newResolver(t, api.TokenReviews()), nil)
	client, _ := serveHealth(t, auth.Methods{
		healthpb.Health_Check_FullMethodName: rule, healthpb.Health_Watch_FullMethodName: rule,
	}, 0)
	both(t, bearing(t, token), client, codes.PermissionDenied)
}

func TestAnAnyAccountRuleAdmitsEveryAccountItsResolverServes(t *testing.T) {
	stranger := auth.Account{Namespace: testNamespace, Name: "test-stranger"}
	_, strangerToken := workload(t, stranger)
	_, outsiderToken := workload(t, auth.Account{Namespace: "test-elsewhere", Name: "test-app"})
	rule := auth.AnyAccount(newResolver(t, api.TokenReviews()))
	client, callers := serveHealth(t, auth.Methods{
		healthpb.Health_Check_FullMethodName: rule, healthpb.Health_Watch_FullMethodName: rule,
	}, 0)
	both(t, bearing(t, ""), client, codes.Unauthenticated)
	both(t, bearing(t, outsiderToken), client, codes.Unauthenticated)
	both(t, bearing(t, strangerToken), client, codes.OK)
	for range 2 {
		if caller := <-callers; caller.Account != stranger {
			t.Errorf("handler saw caller %+v, want %v", caller, stranger)
		}
	}
}

func TestHealthMethodsNeedNoBearer(t *testing.T) {
	client, callers := serveHealth(t, auth.HealthMethods(), 0)
	both(t, bearing(t, ""), client, codes.OK)
	select {
	case caller := <-callers:
		t.Fatalf("a public call carried caller %+v", caller)
	default:
	}
}

func TestTheAdmissionTimeoutBoundsTheReviewAlone(t *testing.T) {
	_, token := workload(t, app)
	reviews := newHeldReviews()
	t.Cleanup(func() { close(reviews.release) })
	rule := auth.Accounts(newResolver(t, reviews), []auth.Account{app})
	client, _ := serveHealth(t, auth.Methods{
		healthpb.Health_Check_FullMethodName: rule, healthpb.Health_Watch_FullMethodName: rule,
	}, 50*time.Millisecond)
	both(t, bearing(t, token), client, codes.DeadlineExceeded)
}

func TestCallerRefusesACallNoAccountRuleAdmitted(t *testing.T) {
	if _, err := auth.Caller(context.Background()); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Caller without an admission: %v", err)
	}
}
