package auth_test

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// callerHealth stands in for a served API: it answers SERVING and records the caller each call was admitted as.
type callerHealth struct {
	healthpb.UnimplementedHealthServer
	callers    chan auth.Principal
	effective  chan auth.ServiceAccountPrincipal
	admissions chan auth.Admission
}

func (h callerHealth) record(ctx context.Context) {
	principal, err := auth.Caller(ctx)
	if err == nil {
		h.callers <- principal
	}
	if h.effective != nil {
		if effective, err := auth.EffectiveCaller(ctx); err == nil {
			h.effective <- effective
		}
	}
	if h.admissions != nil {
		if admission, err := auth.Admitted(ctx); err == nil {
			h.admissions <- admission
		}
	}
}

func (h callerHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	h.record(ctx)
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

func serveAdmissionHealth(t *testing.T, methods auth.Methods) (healthpb.HealthClient, <-chan auth.ServiceAccountPrincipal, <-chan auth.Admission) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	effective := make(chan auth.ServiceAccountPrincipal, 8)
	admissions := make(chan auth.Admission, 8)
	server := grpc.NewServer(methods.ServerOptions(0)...)
	healthpb.RegisterHealthServer(server, callerHealth{effective: effective, admissions: admissions})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return healthpb.NewHealthClient(connection), effective, admissions
}

func delegationContext(t *testing.T, token string, caller auth.Account, uid string) context.Context {
	t.Helper()
	envelope := &delegationpb.Delegation{
		Caller: &delegationpb.ServiceAccountIdentity{
			Account: &sandboxpb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}, Uid: uid,
		},
		ActionRequestId: uuid.NewString(), ActionExecutionId: uuid.NewString(),
	}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	return metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Agentplane-Delegated "+token, auth.DelegationMetadataKey, string(encoded),
	))
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

func TestMethodsWithDelegationRequiresUniqueServedMethodsAndAnIdentityReader(t *testing.T) {
	method := healthpb.Health_Check_FullMethodName
	methods := auth.Methods{method: auth.ReviewedServiceAccount(newResolver(t, api.TokenReviews()))}
	delegator := auth.Delegator{
		Account: auth.Account{Namespace: testNamespace, Name: "test-actions"}, CallerNamespaces: []string{testNamespace},
	}
	reader := auth.ServiceAccountUIDReaderFunc(func(context.Context, auth.Account) (string, bool, error) {
		return "current-uid", true, nil
	})

	for _, test := range []struct {
		name    string
		names   []string
		reader  auth.ServiceAccountUIDReader
		wantErr string
	}{
		{
			name: "duplicate method", names: []string{method, method},
			reader:  reader,
			wantErr: `delegated method "` + method + `" is repeated`,
		},
		{
			name: "unserved method", names: []string{healthpb.Health_Watch_FullMethodName},
			wantErr: `delegated method "` + healthpb.Health_Watch_FullMethodName + `" is not served`,
		},
		{
			name: "missing identity reader", names: []string{method},
			wantErr: `delegated method "` + method + `": ServiceAccount reader and delegators are required`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			delegators := []auth.Delegator{delegator}
			_, err := methods.WithDelegation(test.names, delegators, test.reader)
			if err == nil || err.Error() != test.wantErr {
				t.Fatalf("WithDelegation error = %v, want %q", err, test.wantErr)
			}
		})
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

func TestReviewedServiceAccountDelegationKeepsActorAndEffectiveCallerSeparate(t *testing.T) {
	actor := auth.Account{Namespace: testNamespace, Name: "test-actions"}
	caller := auth.Account{Namespace: testNamespace, Name: "test-caller"}
	_, actorToken := workload(t, actor)
	_, callerToken := workload(t, caller)
	actorAccount, err := api.ServiceAccount(t.Context(), actor)
	if err != nil {
		t.Fatal(err)
	}
	callerAccount, err := api.ServiceAccount(t.Context(), caller)
	if err != nil {
		t.Fatal(err)
	}
	resolver := newResolver(t, api.TokenReviews(), auth.WithVerdictTTL(0))
	reader := auth.ServiceAccountUIDReaderFunc(func(ctx context.Context, account auth.Account) (string, bool, error) {
		serviceAccount, err := api.Clientset.CoreV1().ServiceAccounts(account.Namespace).Get(ctx, account.Name, metav1.GetOptions{})
		if err != nil {
			return "", false, err
		}
		return string(serviceAccount.UID), true, nil
	})
	methods, err := auth.Methods{
		healthpb.Health_Check_FullMethodName: auth.ReviewedServiceAccount(resolver),
		healthpb.Health_Watch_FullMethodName: auth.ReviewedServiceAccount(resolver),
	}.WithDelegation([]string{healthpb.Health_Check_FullMethodName}, []auth.Delegator{{
		Account: actor, CallerNamespaces: []string{testNamespace},
	}}, reader)
	if err != nil {
		t.Fatal(err)
	}
	client, effective, admissions := serveAdmissionHealth(t, methods)

	if _, err := client.Check(bearing(t, callerToken), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatalf("direct call: %v", err)
	}
	directEffective := <-effective
	directAdmission, ok := (<-admissions).(auth.DirectAdmission)
	if !ok || directEffective.Account != caller || directEffective.ServiceAccountUID != string(callerAccount.UID) ||
		directAdmission.Pod == nil || !directAdmission.Pod.IsPodBound() {
		t.Fatalf("direct admission did not preserve caller and Pod proof: effective=%+v admission=%#v", directEffective, directAdmission)
	}
	if _, err := client.Check(delegationContext(t, actorToken, caller, string(callerAccount.UID)), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatalf("delegated call: %v", err)
	}
	delegatedEffective := <-effective
	delegatedAdmission, ok := (<-admissions).(auth.DelegatedAdmission)
	if !ok || delegatedEffective != directEffective || delegatedAdmission.Actor.Account != actor ||
		delegatedAdmission.Actor.ServiceAccountUID != string(actorAccount.UID) || !delegatedAdmission.Actor.IsPodBound() ||
		delegatedAdmission.EffectiveCaller.Account != caller || delegatedAdmission.EffectiveCaller.ServiceAccountUID != string(callerAccount.UID) {
		t.Fatalf("delegated admission conflated actor and caller: effective=%+v admission=%#v", delegatedEffective, delegatedAdmission)
	}
	delegatedContext := delegationContext(t, actorToken, caller, string(callerAccount.UID))
	watch, err := client.Watch(delegatedContext, &healthpb.HealthCheckRequest{})
	if err == nil {
		_, err = watch.Recv()
	}
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("direct-only method accepted delegated metadata: %v", err)
	}
	if _, err := client.List(delegatedContext, &healthpb.HealthListRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("unlisted method was not refused: %v", err)
	}
	// A compatibility receiver rule cannot infer a delegation from an old Bearer token plus
	// caller metadata. Mixed and malformed forms are refused before the handler runs.
	legacyMixed := metadata.AppendToOutgoingContext(bearing(t, callerToken), auth.DelegationMetadataKey, "old-receiver-envelope")
	if _, err := client.Check(legacyMixed, &healthpb.HealthCheckRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("mixed old receiver metadata accepted: %v", err)
	}
	mixedAuthorization := metadata.AppendToOutgoingContext(
		delegationContext(t, actorToken, caller, string(callerAccount.UID)), "authorization", "Bearer "+callerToken,
	)
	if _, err := client.Check(mixedAuthorization, &healthpb.HealthCheckRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("mixed direct and delegated authorization accepted: %v", err)
	}
	malformed := metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
		"authorization", "Agentplane-Delegated "+actorToken, auth.DelegationMetadataKey, "not-a-protobuf",
	))
	if _, err := client.Check(malformed, &healthpb.HealthCheckRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("malformed delegation accepted: %v", err)
	}

	wrongUID := delegationContext(t, actorToken, caller, "stale-account-uid")
	if _, err := client.Check(wrongUID, &healthpb.HealthCheckRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("same-name replacement UID accepted: %v", err)
	}
	missingAccount := auth.ServiceAccountUIDReaderFunc(func(context.Context, auth.Account) (string, bool, error) {
		return "", false, nil
	})
	missingRule, err := auth.ReviewedServiceAccount(resolver).AllowDelegationFrom([]auth.Delegator{{Account: actor, CallerNamespaces: []string{testNamespace}}}, missingAccount)
	if err != nil {
		t.Fatal(err)
	}
	missingClient, _, _ := serveAdmissionHealth(t, auth.Methods{healthpb.Health_Check_FullMethodName: missingRule})
	if _, err := missingClient.Check(delegationContext(t, actorToken, caller, string(callerAccount.UID)), &healthpb.HealthCheckRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("missing caller ServiceAccount admitted: %v", err)
	}
	outside, err := auth.ReviewedServiceAccount(resolver).AllowDelegationFrom([]auth.Delegator{{
		Account: actor, CallerNamespaces: []string{"different-namespace"},
	}}, reader)
	if err != nil {
		t.Fatal(err)
	}
	outsideClient, _, _ := serveAdmissionHealth(t, auth.Methods{healthpb.Health_Check_FullMethodName: outside})
	if _, err := outsideClient.Check(delegationContext(t, actorToken, caller, string(callerAccount.UID)), &healthpb.HealthCheckRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("caller outside the namespace grant admitted: %v", err)
	}
	if _, err := outsideClient.Check(delegationContext(t, callerToken, caller, string(callerAccount.UID)), &healthpb.HealthCheckRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("untrusted actor admitted: %v", err)
	}
	if _, err := outsideClient.Check(delegationContext(t, actorToken, auth.Account{Namespace: "outside", Name: "caller"}, "some-uid"), &healthpb.HealthCheckRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("unserved caller namespace did not fail closed: %v", err)
	}
	unavailableReader := auth.ServiceAccountUIDReaderFunc(func(context.Context, auth.Account) (string, bool, error) {
		return "", false, errors.New("test ServiceAccount API outage")
	})
	unavailableRule, err := auth.ReviewedServiceAccount(resolver).AllowDelegationFrom([]auth.Delegator{{
		Account: actor, CallerNamespaces: []string{testNamespace},
	}}, unavailableReader)
	if err != nil {
		t.Fatal(err)
	}
	unavailableClient, _, _ := serveAdmissionHealth(t, auth.Methods{healthpb.Health_Check_FullMethodName: unavailableRule})
	if _, err := unavailableClient.Check(delegationContext(t, actorToken, caller, string(callerAccount.UID)), &healthpb.HealthCheckRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("ServiceAccount API failure was not unavailable: %v", err)
	}
	unboundCaller, err := api.UnboundToken(t.Context(), caller, "test-dispatcher")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Check(bearing(t, unboundCaller), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatalf("direct account-token call: %v", err)
	}
	accountOnlyAdmission := <-admissions
	if direct, ok := accountOnlyAdmission.(auth.DirectAdmission); !ok || direct.Caller.ServiceAccountUID != string(callerAccount.UID) || direct.Pod != nil {
		t.Fatalf("account-token direct admission manufactured Pod proof: %#v", accountOnlyAdmission)
	}
}

func TestCallerRefusesACallNoAccountRuleAdmitted(t *testing.T) {
	if _, err := auth.Caller(context.Background()); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Caller without an admission: %v", err)
	}
}
