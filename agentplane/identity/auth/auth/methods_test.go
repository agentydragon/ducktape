package auth_test

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
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

func TestMethodsWithDelegationRequiresUniqueServedMethodsAndIdentityGrant(t *testing.T) {
	method := healthpb.Health_Check_FullMethodName
	methods := auth.Methods{method: auth.ReviewedServiceAccountOrIdentity(newResolver(t, api.TokenReviews()))}
	delegator := auth.Delegator{Account: auth.Account{Namespace: testNamespace, Name: "test-actions"}, IdentityCallers: true}
	reader := identityUIDReaderFunc(func(context.Context, string) (string, bool, error) { return "current-uid", true, nil })

	for _, test := range []struct {
		name    string
		names   []string
		grant   bool
		reader  auth.IdentityUIDReader
		wantErr string
	}{
		{
			name: "duplicate method", names: []string{method, method}, grant: true,
			reader:  reader,
			wantErr: `delegated method "` + method + `" is repeated`,
		},
		{
			name: "unserved method", names: []string{healthpb.Health_Watch_FullMethodName},
			wantErr: `delegated method "` + healthpb.Health_Watch_FullMethodName + `" is not served`,
		},
		{
			name: "missing Identity grant", names: []string{method}, reader: reader,
			wantErr: `delegated method "` + method + `": delegator is not granted Identity callers`,
		},
		{
			name: "missing Identity reader", names: []string{method}, grant: true,
			wantErr: `delegated method "` + method + `": delegator may assert Identities, but the receiver resolves none`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			delegator.IdentityCallers = test.grant
			_, err := methods.WithDelegation(test.names, []auth.Delegator{delegator}, test.reader)
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

func connectionIdentityDelegationContext(t *testing.T, token string, identity auth.Identity) context.Context {
	t.Helper()
	encoded, err := auth.MarshalDelegation(&delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: identity.UID}},
		Provenance: &delegationpb.Delegation_Connection{Connection: &delegationpb.ConnectionProvenance{
			ConnectionId: "018f4e32-6632-7e7f-83dd-7f90ccf84f68", GrantId: "018f4e32-6632-7e7f-83dd-7f90ccf84f69",
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Agentplane-Delegated "+token, auth.DelegationMetadataKey, string(encoded),
	))
}

// legacyServiceAccountEnvelope encodes the former field-1 effective ServiceAccount caller using
// protobuf wire primitives. Current generated code reserves and ignores that field, so admission
// must reject it as an envelope with no effective Identity.
func legacyServiceAccountEnvelope() []byte {
	appendBytes := func(target []byte, number protowire.Number, value []byte) []byte {
		target = protowire.AppendTag(target, number, protowire.BytesType)
		return protowire.AppendBytes(target, value)
	}
	account := appendBytes(nil, 1, []byte(testNamespace))
	account = appendBytes(account, 2, []byte("test-caller"))
	serviceAccount := appendBytes(nil, 1, account)
	serviceAccount = appendBytes(serviceAccount, 2, []byte("old-service-account-uid"))
	delegation := appendBytes(nil, 1, serviceAccount)
	action := appendBytes(nil, 1, []byte("11111111-1111-4111-8111-111111111111"))
	action = appendBytes(action, 2, []byte("22222222-2222-4222-8222-222222222222"))
	return appendBytes(delegation, 5, action)
}

func TestLegacyServiceAccountEffectiveCallerEnvelopeFailsClosed(t *testing.T) {
	actor := auth.Account{Namespace: testNamespace, Name: "test-legacy-delegator"}
	_, actorToken := workload(t, actor)
	identities := identityUIDReaderFunc(func(context.Context, string) (string, bool, error) {
		return "identity-uid", true, nil
	})
	rule, err := auth.ReviewedServiceAccountOrIdentity(newResolver(t, api.TokenReviews(), auth.WithVerdictTTL(0))).AllowDelegationFrom(
		[]auth.Delegator{{Account: actor, IdentityCallers: true}}, identities,
	)
	if err != nil {
		t.Fatal(err)
	}
	client, effective, admissions := serveAdmissionHealth(t, auth.Methods{healthpb.Health_Check_FullMethodName: rule})
	ctx := metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
		"authorization", "Agentplane-Delegated "+actorToken,
		auth.DelegationMetadataKey, string(legacyServiceAccountEnvelope()),
	))
	if _, err := client.Check(ctx, &healthpb.HealthCheckRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("legacy ServiceAccount effective caller returned %v, want Unauthenticated", err)
	}
	select {
	case got := <-effective:
		t.Fatalf("legacy envelope reached handler with effective caller %+v", got)
	default:
	}
	select {
	case got := <-admissions:
		t.Fatalf("legacy envelope reached handler with admission %#v", got)
	default:
	}
}

func TestConnectionProvenanceRequiresAnExplicitIdentityDelegationMethod(t *testing.T) {
	const policyNamespace = "test-connection-delegation-policies"
	actor := auth.Account{Namespace: testNamespace, Name: "test-connection-actor"}
	_, actorToken := workload(t, actor)
	_, strangerToken := workload(t, auth.Account{Namespace: testNamespace, Name: "test-connection-stranger"})
	actorAccount, err := api.ServiceAccount(t.Context(), actor)
	if err != nil {
		t.Fatal(err)
	}
	named := workloadauthtesting.NewIdentityWorkload(t, api, testNamespace, "test-connection-identity", policyNamespace)
	identity := auth.Identity{Name: named.Identity.Name, UID: string(named.Identity.UID)}
	identities, err := auth.KubernetesIdentityUIDs(named.Objects, policyNamespace)
	if err != nil {
		t.Fatal(err)
	}
	resolver := newResolver(t, api.TokenReviews(), auth.WithVerdictTTL(0))
	rule, err := auth.ReviewedServiceAccountOrIdentity(resolver).AllowDelegationFrom([]auth.Delegator{{
		Account: actor, IdentityCallers: true, ConnectionProvenance: true,
	}}, identities)
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{
		healthpb.Health_Check_FullMethodName: rule,
		healthpb.Health_Watch_FullMethodName: auth.ReviewedServiceAccountOrIdentity(resolver),
	}
	client, effective, admissions := serveAdmissionHealth(t, methods)
	ctx := connectionIdentityDelegationContext(t, actorToken, identity)
	if _, err := client.Check(ctx, &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatalf("Connection Identity delegation: %v", err)
	}
	got, ok := (<-admissions).(auth.DelegatedIdentityAdmission)
	if !ok || got.Actor.Account != actor || got.Actor.ServiceAccountUID != string(actorAccount.UID) ||
		got.EffectiveCaller != identity || got.Provenance.GetConnection().GetGrantId() != "018f4e32-6632-7e7f-83dd-7f90ccf84f69" {
		t.Fatalf("Connection delegation admission = %#v", got)
	}
	select {
	case effectiveCaller := <-effective:
		t.Fatalf("Identity delegation exposed a ServiceAccount effective caller: %+v", effectiveCaller)
	default:
	}
	if _, err := client.Check(connectionIdentityDelegationContext(t, strangerToken, identity), &healthpb.HealthCheckRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("untrusted actor Connection delegation = %v, want PermissionDenied", err)
	}
	watch, err := client.Watch(ctx, &healthpb.HealthCheckRequest{})
	if err == nil {
		_, err = watch.Recv()
	}
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Connection provenance reached a method without its explicit grant: %v", err)
	}
}

// A trusted actor names an Identity: the method admits it as that exact live incarnation, separate
// from the actor, only when its direct mode admits Identity callers and the actor holds the
// separate Identity grant.
func TestDelegatedIdentityIsAdmittedOnlyAsItsLiveIncarnation(t *testing.T) {
	const policyNamespace = "test-delegation-policies"
	actor := auth.Account{Namespace: testNamespace, Name: "test-identity-actions"}
	_, actorToken := workload(t, actor)
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
	resolver := newResolver(t, api.TokenReviews(), auth.WithVerdictTTL(0))
	methods, err := auth.Methods{
		healthpb.Health_Check_FullMethodName: auth.ReviewedServiceAccountOrIdentity(resolver),
		healthpb.Health_Watch_FullMethodName: auth.ReviewedServiceAccount(resolver),
	}.WithDelegation(
		[]string{healthpb.Health_Check_FullMethodName, healthpb.Health_Watch_FullMethodName},
		[]auth.Delegator{{Account: actor, IdentityCallers: true}}, identities,
	)
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
			[]auth.Delegator{{Account: actor, IdentityCallers: true}}, outage,
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
			[]auth.Delegator{{Account: actor, IdentityCallers: true}}, nil,
		); err == nil {
			t.Fatal("an Identity grant was configured on a receiver that resolves no Identity")
		}
	})
}

type identityUIDReaderFunc func(context.Context, string) (string, bool, error)

func (read identityUIDReaderFunc) GetIdentityUID(ctx context.Context, name string) (string, bool, error) {
	return read(ctx, name)
}
