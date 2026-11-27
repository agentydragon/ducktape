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
		_, err := createSessionForCaller(queueStore, t.Context(), id, sandbox.Name, sandbox.UID, spec, nil, identity, store.ServiceCaller{SPIFFEID: peertls.AppURI})
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
	if attribution, ok := row.Attribution.(*store.ServiceAttribution); !ok || attribution.ActorSPIFFEID != peertls.AppURI || attribution.Identity != nil {
		t.Fatalf("service creator attribution: %#v", row.Attribution)
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

// Sessions and Dispatches written before service and Identity attribution carry only the retired
// ServiceAccount columns. They read as unattributed rather than failing every read that meets them.
func TestUnattributedRowsStayReadable(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 3)
	const sessionID, commandID, sandboxName = "test-legacy-session", "test-legacy-command", "test-legacy-sandbox"
	if _, err := enqueueWithSession(t, queueStore, ctx, sessionID, sandboxName, sandboxUID, input(commandID, "test legacy input")); err != nil {
		t.Fatal(err)
	}
	if _, err := queueStore.DB.ExecContext(ctx, `UPDATE session SET service_attribution=NULL WHERE session_id=$1`, sessionID); err != nil {
		t.Fatal("seed unattributed Session:", err)
	}
	if _, err := queueStore.DB.ExecContext(ctx, `UPDATE dispatch SET service_attribution=NULL WHERE command_id=$1`, commandID); err != nil {
		t.Fatal("seed unattributed Dispatch:", err)
	}
	if row, err := queueStore.GetSession(ctx, sessionID); err != nil || row.Attribution != nil {
		t.Fatalf("store read legacy Session: %+v %v", row, err)
	}
	if row, err := queueStore.GetDispatch(ctx, commandID); err != nil || row.Attribution != nil {
		t.Fatalf("store read legacy Dispatch: %+v %v", row, err)
	}

	service := &Service{Store: queueStore, DispatcherServices: sets.New(peertls.AppURI)}
	if got, err := service.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: sessionID}); err != nil || got.GetSession().GetSessionId() != sessionID {
		t.Fatalf("GetSession: %v, %v", got, err)
	}
	if got, err := service.ListSessions(ctx, &dispatcherpb.ListSessionsRequest{}); err != nil || len(got.GetSessions()) != 1 || got.GetSessions()[0].GetSessionId() != sessionID {
		t.Fatalf("ListSessions: %v, %v", got, err)
	}
	if got, err := service.GetDispatch(ctx, &dispatcherpb.GetDispatchRequest{CommandId: commandID}); err != nil || got.GetDispatch().GetCommandId() != commandID {
		t.Fatalf("GetDispatch: %v, %v", got, err)
	}
	if got, err := service.ListDispatches(ctx, &dispatcherpb.ListDispatchesRequest{Filter: testSessionDispatchFilter(sessionID)}); err != nil || len(got.GetDispatches()) != 1 || got.GetDispatches()[0].GetCommandId() != commandID {
		t.Fatalf("ListDispatches: %v, %v", got, err)
	}
	if got, err := service.ListSessionDispatches(ctx, &dispatcherpb.ListSessionDispatchesRequest{SessionId: sessionID}); err != nil || len(got.GetDispatches()) != 1 || got.GetDispatches()[0].GetCommandId() != commandID {
		t.Fatalf("ListSessionDispatches: %v, %v", got, err)
	}
	watch := startVersionWatch(t, ctx, queueStore, testWatchHub(t, queueStore), testSessionDispatchFilter(sessionID), "")
	if got := watch.next(t).GetDispatch(); got.GetCommandId() != commandID {
		t.Fatalf("WatchDispatches snapshot = %v, want %s", got, commandID)
	}
	if watch.next(t).GetCaughtUp() == nil {
		t.Fatal("WatchDispatches did not finish its snapshot")
	}
	watch.stop(t)
}
