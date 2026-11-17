package dispatcher

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/access"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
)

type serviceIdentityReader map[string]string

func (r serviceIdentityReader) GetIdentityUID(_ context.Context, name string) (string, bool, error) {
	uid, ok := r[name]
	return uid, ok, nil
}

func TestServiceActorsRetainSeparateProofAndIdentityScope(t *testing.T) {
	owner := &identitypb.Identity{Name: "owner", Uid: uuid.NewString()}
	other := &identitypb.Identity{Name: "other", Uid: uuid.NewString()}
	queueStore := testStore(t, 10)
	sandbox := SandboxSnapshot{Name: "service-target", Namespace: "test", UID: uuid.NewString()}
	inventory := &authorityInventory{sandbox: sandbox, identity: owner}
	seed := func(identity *identitypb.Identity) string {
		t.Helper()
		id := uuid.NewString()
		spec := &sessionpb.SessionSpec{}
		testPlacement(spec, sandbox.Name, sandbox.UID)
		_, err := queueStore.CreateSessionWithPlacementForCaller(t.Context(), id, sandbox.Name, sandbox.UID, spec, nil, nil, store.SessionExecution{Identity: identity}, store.ServiceCaller{SPIFFEID: peertls.AppURI})
		if err != nil {
			t.Fatal(err)
		}
		return id
	}
	owned, foreign := seed(owner), seed(other)
	row, err := queueStore.GetSession(t.Context(), owned)
	if err != nil {
		t.Fatal(err)
	}
	if attribution, ok := row.Attribution.(*store.ServiceAttribution); !ok || attribution.ActorSPIFFEID != peertls.AppURI || row.Creator != nil {
		t.Fatalf("service creator acquired account proof: %#v", row)
	}
	rule, err := auth.Services([]string{peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(rule)
	methods, err = methods.WithServiceDelegation([]string{dispatcherpb.SessionDispatcher_SubmitInput_FullMethodName, dispatcherpb.SessionDispatcher_GetSession_FullMethodName, dispatcherpb.SessionDispatcher_CreateSession_FullMethodName}, []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}, serviceIdentityReader{owner.Name: owner.Uid, other.Name: other.Uid})
	if err != nil {
		t.Fatal(err)
	}
	rules := access.NewIndex()
	ownerObject := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Name: owner.Name, UID: types.UID(owner.Uid)}}
	rules.PutIdentity(ownerObject)
	connection := grpcmethods.ServeTLSWithTimeout(t, time.Second, peertls.SessionDispatcherURI, methods, func(server *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(server, &Service{Store: queueStore, Sandboxes: inventory, DispatcherServices: sets.New(peertls.AppURI), EnvironmentRules: rules, PolicyReady: func() error { return nil }})
	}, peertls.AppURI, peertls.ActionsURI, peertls.SessionDispatcherURI)
	client := func(serviceID string) dispatcherpb.SessionDispatcherClient {
		t.Helper()
		return dispatcherpb.NewSessionDispatcherClient(grpcmethods.AsCaller(connection, serviceID))
	}
	app, actions, denied := client(peertls.AppURI), client(peertls.ActionsURI), client(peertls.SessionDispatcherURI)
	if _, err := app.GetSession(t.Context(), &dispatcherpb.GetSessionRequest{SessionId: foreign}); err != nil {
		t.Fatalf("direct component read: %v", err)
	}
	if _, err := denied.GetSession(t.Context(), &dispatcherpb.GetSessionRequest{SessionId: owned}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("unlisted combined Gateway service read: %v", err)
	}
	action := &delegationpb.ActionProvenance{RequestId: uuid.NewString(), ExecutionId: uuid.NewString()}
	envelope := &delegationpb.Delegation{EffectiveCaller: &delegationpb.Delegation_Identity{Identity: owner}, Provenance: &delegationpb.Delegation_Action{Action: action}}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	delegated := metadata.NewOutgoingContext(t.Context(), metadata.Pairs(auth.DelegationMetadataKey, string(encoded)))
	commandID := uuid.NewString()
	if _, err := actions.SubmitInput(delegated, &dispatcherpb.SubmitInputRequest{CommandId: commandID, SessionId: owned, Text: "delegated service"}); err != nil {
		t.Fatal(err)
	}
	queued, err := queueStore.GetDispatch(t.Context(), commandID)
	if err != nil {
		t.Fatal(err)
	}
	attribution, ok := queued.Attribution.(*store.ServiceAttribution)
	if !ok || attribution.ActorSPIFFEID != peertls.ActionsURI || !store.SameIdentity(attribution.Identity, owner) || attribution.Action.RequestID != action.RequestId {
		t.Fatalf("delegated service proof: %#v", queued.Attribution)
	}
	if wake, ok := store.WakeCaller(queued.Attribution).(lifecycle.ServiceWakeCaller); !ok || wake.SPIFFEID != peertls.ActionsURI {
		t.Fatalf("Action attribution did not use the Actions wake broker: %#v", store.WakeCaller(queued.Attribution))
	}
	if _, err := actions.GetSession(delegated, &dispatcherpb.GetSessionRequest{SessionId: foreign}); status.Code(err) != codes.NotFound {
		t.Fatalf("delegator inherited component authority: %v", err)
	}
	if _, err := actions.CreateSession(delegated, nativeCreateRequest(uuid.NewString(), sandbox.Name, sandbox.UID)); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ungranted delegated lifecycle: %v", err)
	}
	appRequest := nativeCreateRequest(uuid.NewString(), sandbox.Name, sandbox.UID)
	for range 2 {
		if _, err := app.CreateSession(t.Context(), appRequest); err != nil {
			t.Fatalf("direct service native creation/receipt: %v", err)
		}
	}
	addEnvironmentGrant(rules, "service-bind-session", ownerObject, "bind_session", policyapi.EnvironmentSelector{
		BoundIdentity: &policyapi.BoundIdentitySelector{Self: true},
	}, nil)
	request := nativeCreateRequest(uuid.NewString(), sandbox.Name, sandbox.UID)
	if _, err := actions.CreateSession(delegated, request); err != nil {
		t.Fatalf("granted service-delegated native creation: %v", err)
	}
	created, err := queueStore.GetSession(t.Context(), request.SessionId)
	if err != nil {
		t.Fatal(err)
	}
	creator, ok := created.Attribution.(*store.ServiceAttribution)
	if !ok || creator.ActorSPIFFEID != peertls.ActionsURI || !store.SameIdentity(creator.Identity, owner) || creator.Action.RequestID != action.RequestId {
		t.Fatalf("service-delegated creator proof: %#v", created.Attribution)
	}
	rules.DeleteBinding("service-bind-session")
	if _, err := actions.CreateSession(delegated, request); err != nil {
		t.Fatalf("service-delegated receipt after grant revocation: %v", err)
	}
	ambiguous := metadata.AppendToOutgoingContext(delegated, "authorization", "Bearer legacy")
	if _, err := actions.GetSession(ambiguous, &dispatcherpb.GetSessionRequest{SessionId: owned}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("bearer fallback: %v", err)
	}
}
