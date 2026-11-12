package auth_test

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	authlistener "github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
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
	effective  chan *auth.ServiceAccountPrincipal
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

func serveAdmissionHealth(t *testing.T, methods auth.Methods) (healthpb.HealthClient, <-chan *auth.ServiceAccountPrincipal, <-chan auth.Admission) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	effective := make(chan *auth.ServiceAccountPrincipal, 8)
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
		EffectiveCaller: &delegationpb.Delegation_Caller{Caller: &identitypb.ServiceAccountIdentity{
			Account: &identitypb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}, Uid: uid,
		}},
		Provenance: &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{RequestId: uuid.NewString(), ExecutionId: uuid.NewString()}},
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
			name: "missing ServiceAccount reader", names: []string{method},
			wantErr: `delegated method "` + method + `": delegator may assert ServiceAccounts, but the receiver resolves none`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			delegators := []auth.Delegator{delegator}
			_, err := methods.WithDelegation(test.names, delegators, test.reader, nil)
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
	}}, reader, nil)
	if err != nil {
		t.Fatal(err)
	}
	client, effective, admissions := serveAdmissionHealth(t, methods)

	if _, err := client.Check(bearing(t, callerToken), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatalf("direct call: %v", err)
	}
	directEffective := <-effective
	directAdmission, ok := (<-admissions).(auth.DirectAdmission)
	directAccount := directEffective.GetAccount()
	if !ok || directAccount.GetNamespace() != caller.Namespace || directAccount.GetName() != caller.Name || directEffective.GetUid() != string(callerAccount.UID) ||
		directAdmission.Pod == nil || !directAdmission.Pod.IsPodBound() {
		t.Fatalf("direct admission did not preserve caller and Pod proof: effective=%+v admission=%#v", directEffective, directAdmission)
	}
	if _, err := client.Check(delegationContext(t, actorToken, caller, string(callerAccount.UID)), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatalf("delegated call: %v", err)
	}
	delegatedEffective := <-effective
	delegatedAdmission, ok := (<-admissions).(auth.DelegatedAdmission)
	delegatedAccount := delegatedEffective.GetAccount()
	if !ok || directAccount.GetNamespace() != delegatedAccount.GetNamespace() || directAccount.GetName() != delegatedAccount.GetName() ||
		delegatedEffective.GetUid() != directEffective.GetUid() || delegatedAdmission.Actor.Account != actor ||
		delegatedAdmission.Actor.ServiceAccountUID != string(actorAccount.UID) || !delegatedAdmission.Actor.IsPodBound() ||
		delegatedAdmission.EffectiveCaller.GetAccount().GetNamespace() != caller.Namespace ||
		delegatedAdmission.EffectiveCaller.GetAccount().GetName() != caller.Name || delegatedAdmission.EffectiveCaller.GetUid() != string(callerAccount.UID) {
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
	missingRule, err := auth.ReviewedServiceAccount(resolver).AllowDelegationFrom([]auth.Delegator{{Account: actor, CallerNamespaces: []string{testNamespace}}}, missingAccount, nil)
	if err != nil {
		t.Fatal(err)
	}
	missingClient, _, _ := serveAdmissionHealth(t, auth.Methods{healthpb.Health_Check_FullMethodName: missingRule})
	if _, err := missingClient.Check(delegationContext(t, actorToken, caller, string(callerAccount.UID)), &healthpb.HealthCheckRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("missing caller ServiceAccount admitted: %v", err)
	}
	outside, err := auth.ReviewedServiceAccount(resolver).AllowDelegationFrom([]auth.Delegator{{
		Account: actor, CallerNamespaces: []string{"different-namespace"},
	}}, reader, nil)
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
	}}, unavailableReader, nil)
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
	if direct, ok := accountOnlyAdmission.(auth.DirectAdmission); !ok || direct.Caller.GetUid() != string(callerAccount.UID) || direct.Pod != nil {
		t.Fatalf("account-token direct admission manufactured Pod proof: %#v", accountOnlyAdmission)
	}
}

func TestDelegationUsesTheConfiguredListenerModeAndMethodGrant(t *testing.T) {
	actor := auth.Account{Namespace: testNamespace, Name: "test-actions"}
	caller := auth.Account{Namespace: testNamespace, Name: "test-caller"}
	actorPod, actorToken := workload(t, actor)
	_, _ = workload(t, caller)
	actorAccount, err := api.Clientset.CoreV1().ServiceAccounts(actor.Namespace).Get(t.Context(), actor.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	callerAccount, err := api.Clientset.CoreV1().ServiceAccounts(caller.Namespace).Get(t.Context(), caller.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	reviews := &kubeapiserver.CountedReviews{TokenReviews: api.TokenReviews()}
	baseResolver := newResolver(t, reviews, auth.WithVerdictTTL(0))
	reader := auth.ServiceAccountUIDReaderFunc(func(ctx context.Context, account auth.Account) (string, bool, error) {
		serviceAccount, err := api.Clientset.CoreV1().ServiceAccounts(account.Namespace).Get(ctx, account.Name, metav1.GetOptions{})
		if err != nil {
			return "", false, err
		}
		return string(serviceAccount.UID), true, nil
	})
	methodsFor := func(t *testing.T, config authlistener.Config, svid *auth.SVIDResolver) auth.Methods {
		t.Helper()
		resolver, err := baseResolver.WithListenerAuthentication(config, svid)
		if err != nil {
			t.Fatal(err)
		}
		methods, err := auth.Methods{
			healthpb.Health_Check_FullMethodName: auth.ReviewedServiceAccount(resolver),
			healthpb.Health_Watch_FullMethodName: auth.ReviewedServiceAccount(resolver),
		}.WithDelegation([]string{healthpb.Health_Check_FullMethodName}, []auth.Delegator{{
			Account: actor, CallerNamespaces: []string{testNamespace},
		}}, reader, nil)
		if err != nil {
			t.Fatal(err)
		}
		return methods
	}
	delegated := delegationContext(t, actorToken, caller, string(callerAccount.UID))

	t.Run("SPIFFE-only listener refuses delegated bearer before review", func(t *testing.T) {
		objects, sandbox, _ := svidFixture(t)
		svids, err := auth.NewSVIDResolver(objects, []string{sandbox.Namespace}, nil)
		if err != nil {
			t.Fatal(err)
		}
		methods := methodsFor(t, authlistener.Config{
			ListenAddress: "test",
			TLS:           &authlistener.TLS{CertFile: "/test/tls.crt", KeyFile: "/test/tls.key"},
			Auth:          []authlistener.Authentication{&authlistener.SPIFFEAuth{CAFile: "/test/ca.pem"}},
		}, svids)
		client, effective, admissions := serveAdmissionHealth(t, methods)
		if _, err := client.Check(delegated, &healthpb.HealthCheckRequest{}); status.Code(err) != codes.Unauthenticated {
			t.Fatalf("delegated bearer on SPIFFE-only listener returned %v", err)
		}
		if calls := reviews.Calls.Load(); calls != 0 {
			t.Fatalf("listener refused after %d TokenReviews; want refusal before review", calls)
		}
		select {
		case got := <-effective:
			t.Fatalf("refused delegation reached handler with effective caller %+v", got)
		default:
		}
		select {
		case got := <-admissions:
			t.Fatalf("refused delegation reached handler with admission %#v", got)
		default:
		}
	})

	t.Run("bearer-enabled method admits only its delegated grant", func(t *testing.T) {
		methods := methodsFor(t, authlistener.Config{
			ListenAddress: "test",
			Auth: []authlistener.Authentication{&authlistener.KubernetesBearerAuth{
				Audiences: []string{testAudience},
			}},
		}, nil)
		client, effective, admissions := serveAdmissionHealth(t, methods)
		if _, err := client.Check(delegated, &healthpb.HealthCheckRequest{}); err != nil {
			t.Fatalf("delegated call on bearer-enabled method: %v", err)
		}
		gotCaller := <-effective
		gotAdmission, ok := (<-admissions).(auth.DelegatedAdmission)
		if gotCaller.GetAccount().GetNamespace() != caller.Namespace || gotCaller.GetAccount().GetName() != caller.Name ||
			gotCaller.GetUid() != string(callerAccount.UID) || !ok || gotAdmission.Actor.Account != actor ||
			gotAdmission.Actor.ServiceAccountUID != string(actorAccount.UID) || gotAdmission.Actor.PodUID != string(actorPod.UID) ||
			!gotAdmission.Actor.IsPodBound() || gotAdmission.EffectiveCaller.GetUid() != string(callerAccount.UID) {
			t.Fatalf("delegated admission lost actor/effective caller provenance: effective=%+v admission=%#v", gotCaller, gotAdmission)
		}
		if reviews.Calls.Load() != 1 {
			t.Fatalf("delegated actor review count = %d, want one", reviews.Calls.Load())
		}
		watch, err := client.Watch(delegated, &healthpb.HealthCheckRequest{})
		if err == nil {
			_, err = watch.Recv()
		}
		if status.Code(err) != codes.Unauthenticated {
			t.Fatalf("delegation on a direct-only method returned %v", err)
		}
		if reviews.Calls.Load() != 1 {
			t.Fatalf("direct-only method reviewed delegated actor; calls=%d", reviews.Calls.Load())
		}
		select {
		case got := <-effective:
			t.Fatalf("direct-only method reached handler with effective caller %+v", got)
		default:
		}
	})
}

func TestCallerRefusesACallNoAccountRuleAdmitted(t *testing.T) {
	if _, err := auth.Caller(context.Background()); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Caller without an admission: %v", err)
	}
}

func identityDelegationContext(t *testing.T, token string, identity auth.Identity) context.Context {
	t.Helper()
	encoded, err := auth.MarshalDelegation(&delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: identity.UID}},
		Provenance:      &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{RequestId: uuid.NewString(), ExecutionId: uuid.NewString()}},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	t.Cleanup(cancel)
	return metadata.NewOutgoingContext(ctx, metadata.Pairs(
		"authorization", "Agentplane-Delegated "+token, auth.DelegationMetadataKey, string(encoded),
	))
}

// A trusted actor names an Identity: the method admits it as that exact live incarnation, separate
// from the actor, only when its direct mode admits Identity callers and the actor holds the
// separate Identity grant.
func TestDelegatedIdentityIsAdmittedOnlyAsItsLiveIncarnation(t *testing.T) {
	const policyNamespace = "test-delegation-policies"
	actor := auth.Account{Namespace: testNamespace, Name: "test-identity-actions"}
	accountsOnly := auth.Account{Namespace: testNamespace, Name: "test-account-actions"}
	_, actorToken := workload(t, actor)
	_, accountsOnlyToken := workload(t, accountsOnly)
	_, strangerToken := workload(t, auth.Account{Namespace: testNamespace, Name: "test-untrusted-actor"})
	actorAccount, err := api.ServiceAccount(t.Context(), actor)
	if err != nil {
		t.Fatal(err)
	}
	named := workloadauthtesting.NewIdentityWorkload(t, api, testNamespace, "test-delegated-identity", policyNamespace)
	identity := auth.Identity{Name: named.Identity.Name, UID: string(named.Identity.UID)}
	identities, err := auth.KubernetesIdentityUIDs(named.Objects, policyNamespace)
	if err != nil {
		t.Fatal(err)
	}
	accounts := auth.ServiceAccountUIDReaderFunc(func(context.Context, auth.Account) (string, bool, error) {
		return "", false, nil
	})
	resolver := newResolver(t, api.TokenReviews(), auth.WithVerdictTTL(0))
	methods, err := auth.Methods{
		healthpb.Health_Check_FullMethodName: auth.ReviewedServiceAccountOrIdentity(resolver),
		healthpb.Health_Watch_FullMethodName: auth.ReviewedServiceAccount(resolver),
	}.WithDelegation([]string{healthpb.Health_Check_FullMethodName, healthpb.Health_Watch_FullMethodName}, []auth.Delegator{
		{Account: actor, IdentityCallers: true},
		{Account: accountsOnly, CallerNamespaces: []string{testNamespace}},
	}, accounts, identities)
	if err != nil {
		t.Fatal(err)
	}
	client, effective, admissions := serveAdmissionHealth(t, methods)

	if _, err := client.Check(identityDelegationContext(t, actorToken, identity), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatalf("delegated Identity call: %v", err)
	}
	admitted, ok := (<-admissions).(auth.DelegatedIdentityAdmission)
	if !ok || admitted.EffectiveCaller != identity || admitted.Actor.Account != actor ||
		admitted.Actor.ServiceAccountUID != string(actorAccount.UID) || !admitted.Actor.IsPodBound() ||
		admitted.Provenance.GetIdentity().GetName() != identity.Name {
		t.Fatalf("delegated Identity admission = %#v, want %v acting for %v", admitted, actor, identity)
	}
	select {
	case got := <-effective:
		t.Fatalf("a delegated Identity read as the ServiceAccount effective caller %v", got)
	default:
	}

	refused := func(t *testing.T, ctx context.Context, want codes.Code) {
		t.Helper()
		if _, err := client.Check(ctx, &healthpb.HealthCheckRequest{}); status.Code(err) != want {
			t.Fatalf("Check = %v, want %v", err, want)
		}
		select {
		case got := <-admissions:
			t.Fatalf("refused delegation reached the handler as %#v", got)
		default:
		}
	}
	t.Run("an actor without the Identity grant", func(t *testing.T) {
		refused(t, identityDelegationContext(t, accountsOnlyToken, identity), codes.PermissionDenied)
	})
	t.Run("an actor without a delegation grant", func(t *testing.T) {
		refused(t, identityDelegationContext(t, strangerToken, identity), codes.PermissionDenied)
	})
	t.Run("a method admitting no Identity caller", func(t *testing.T) {
		watch, err := client.Watch(identityDelegationContext(t, actorToken, identity), &healthpb.HealthCheckRequest{})
		if err == nil {
			_, err = watch.Recv()
		}
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("Watch = %v, want PermissionDenied", err)
		}
	})
	t.Run("a stale UID", func(t *testing.T) {
		refused(t, identityDelegationContext(t, actorToken, auth.Identity{Name: identity.Name, UID: uuid.NewString()}), codes.PermissionDenied)
	})
	t.Run("a missing Identity", func(t *testing.T) {
		refused(t, identityDelegationContext(t, actorToken, auth.Identity{Name: "test-absent-identity", UID: uuid.NewString()}), codes.PermissionDenied)
	})
	t.Run("a same-named successor", func(t *testing.T) {
		if err := named.Objects.Delete(t.Context(), named.Identity); err != nil {
			t.Fatal(err)
		}
		successor := &policyapi.Identity{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: identity.Name},
			Spec:       policyapi.IdentitySpec{Description: "successor of " + identity.Name},
		}
		if err := named.Objects.Create(t.Context(), successor); err != nil {
			t.Fatal(err)
		}
		refused(t, identityDelegationContext(t, actorToken, identity), codes.PermissionDenied)
		if _, err := client.Check(identityDelegationContext(t, actorToken, auth.Identity{Name: successor.Name, UID: string(successor.UID)}), &healthpb.HealthCheckRequest{}); err != nil {
			t.Fatalf("delegated call for the successor incarnation: %v", err)
		}
		if got, ok := (<-admissions).(auth.DelegatedIdentityAdmission); !ok || got.EffectiveCaller.UID != string(successor.UID) {
			t.Fatalf("successor admission = %#v", got)
		}
	})
	t.Run("an unreadable Identity", func(t *testing.T) {
		outage := identityUIDReaderFunc(func(context.Context, string) (string, bool, error) {
			return "", false, errors.New("test Identity API outage")
		})
		rule, err := auth.ReviewedServiceAccountOrIdentity(resolver).AllowDelegationFrom(
			[]auth.Delegator{{Account: actor, IdentityCallers: true}}, accounts, outage,
		)
		if err != nil {
			t.Fatal(err)
		}
		outageClient, _, _ := serveAdmissionHealth(t, auth.Methods{healthpb.Health_Check_FullMethodName: rule})
		if _, err := outageClient.Check(identityDelegationContext(t, actorToken, identity), &healthpb.HealthCheckRequest{}); status.Code(err) != codes.Unavailable {
			t.Fatalf("Identity API failure = %v, want Unavailable", err)
		}
	})
	t.Run("an Identity grant needs a receiver resolving Identities", func(t *testing.T) {
		if _, err := auth.ReviewedServiceAccountOrIdentity(resolver).AllowDelegationFrom(
			[]auth.Delegator{{Account: actor, IdentityCallers: true}}, accounts, nil,
		); err == nil {
			t.Fatal("an Identity grant was configured on a receiver that resolves no Identity")
		}
	})
}

type identityUIDReaderFunc func(context.Context, string) (string, bool, error)

func (read identityUIDReaderFunc) GetIdentityUID(ctx context.Context, name string) (string, bool, error) {
	return read(ctx, name)
}
