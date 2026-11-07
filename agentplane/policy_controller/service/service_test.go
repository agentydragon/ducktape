package service

import (
	"context"
	"encoding/json"
	"net"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/emptypb"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type reviews struct{ revoked atomic.Bool }

func (r *reviews) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	if r.revoked.Load() {
		return &authenticationv1.TokenReview{}, nil
	}
	name := review.Spec.Token
	if name != "app" && name != "alice" {
		return &authenticationv1.TokenReview{}, nil
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{Authenticated: true, Audiences: review.Spec.Audiences, User: authenticationv1.UserInfo{Username: "system:serviceaccount:tools:" + name, UID: "uid-" + name}}}, nil
}

func fixture(t *testing.T, objects ...client.Object) (policypb.PolicyServiceClient, client.WithWatch, *reviews) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kube := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
	service, err := New("policies", kube, []auth.Account{{Namespace: "tools", Name: "app"}})
	if err != nil {
		t.Fatal(err)
	}
	reviews := &reviews{}
	resolver, err := auth.NewResolver(reviews, "policy", []string{"tools"}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(Methods(resolver).ServerOptions(time.Second)...)
	policypb.RegisterPolicyServiceServer(server, service)
	go server.Serve(listener)
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return policypb.NewPolicyServiceClient(conn), kube, reviews
}

func tokenContext(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	return metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer "+token))
}

func binding(name, subject string, sets ...string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: name, UID: types.UID("uid-" + name), Generation: 1}, Spec: policyapi.PolicyBindingSpec{Subject: policyapi.ServiceAccountRef{Namespace: "tools", Name: subject}, PolicySets: sets}}
}

func policySet(name string) *policyapi.PolicySet {
	return &policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: name, UID: types.UID("uid-" + name), Generation: 1}, Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: "action_auto_approve", Policy: &policyapi.ActionPolicy{Type: "exact_actions", Actions: map[string]policyapi.ActionNames{"group": {"inspect", "write"}}}}}}}
}

func TestBindingMessagePreservesOwnerReferences(t *testing.T) {
	controller := true
	source := binding("worker", "worker", "base")
	source.OwnerReferences = []metav1.OwnerReference{
		{APIVersion: "v1", Kind: "Sandbox", Name: "worker", UID: "sandbox-uid", Controller: &controller},
		{APIVersion: "batch/v1", Kind: "Job", Name: "cleanup", UID: "job-uid"},
	}
	message, err := bindingMessage(source)
	if err != nil {
		t.Fatal(err)
	}
	owners := message.GetOwnerReferences()
	if len(owners) != 2 || owners[0].GetApiVersion() != "v1" || owners[0].GetKind() != "Sandbox" ||
		owners[0].GetName() != "worker" || owners[0].GetUid() != "sandbox-uid" || !owners[0].GetController() ||
		owners[1].GetUid() != "job-uid" || owners[1].GetController() {
		t.Fatalf("PolicyBinding owner references = %v", owners)
	}
}

func TestGeneratedSpecPreservesCRDJSON(t *testing.T) {
	source := policySet("base")
	source.Spec.Rules = append(
		source.Spec.Rules,
		policyapi.Rule{Type: "egress", Hosts: []string{"example.test"}, Methods: []string{"GET"}, CredentialRef: &policyapi.CredentialRef{Name: "credential"}, ClusterInternal: true},
		policyapi.Rule{Type: "kubernetes_role_binding", Role: &policyapi.RoleRef{Kind: "Role", Name: "reader"}, Namespaces: []string{"tools"}},
		policyapi.Rule{Type: "session_inbox_access", Operations: []string{"send_message"}, Targets: &policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{{Name: "worker", UID: "worker-uid"}}, InboxTags: []string{"tag"}}},
		policyapi.Rule{Type: "action_auto_approve", Policy: &policyapi.ActionPolicy{Type: "argument_schema", Actions: map[string]policyapi.ActionNames{"group": {"inspect"}}, Schema: &runtime.RawExtension{Raw: []byte(`{"type":"object","required":["owner"],"properties":{"owner":{"const":"team"}}}`)}}},
	)
	message, err := policySetMessage(source)
	if err != nil {
		t.Fatal(err)
	}
	original, err := json.Marshal(source.Spec)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := protojson.Marshal(message.Spec)
	if err != nil {
		t.Fatal(err)
	}
	var a, b any
	if err := json.Unmarshal(original, &a); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(encoded, &b); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("CRD spec %s differs from protobuf %s", original, encoded)
	}
}

func TestRevokedCredentialEndsWatchBeforeAnotherFrame(t *testing.T) {
	api, kube, reviews := fixture(t, binding("one", "alice", "base"))
	stream, err := api.WatchPolicyBindings(tokenContext(t, "alice"), &policypb.WatchPolicyBindingsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatal(err)
	}
	reviews.revoked.Store(true)
	if err := kube.Delete(context.Background(), binding("one", "alice", "base")); err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("revoked watch: %v", err)
	}
}

func TestBindingWatchScopeAndChanges(t *testing.T) {
	otherNamespace := binding("other-namespace", "alice", "base")
	otherNamespace.Namespace = "other-policies"
	api, kube, _ := fixture(t, binding("one", "alice", "base"), binding("foreign", "bob", "private"), otherNamespace)
	stream, err := api.WatchPolicyBindings(tokenContext(t, "alice"), &policypb.WatchPolicyBindingsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if got := first.GetSnapshot().GetBindings(); len(got) != 1 || got[0].GetIdentity().GetNamespace() != "policies" || got[0].GetIdentity().GetName() != "one" {
		t.Fatalf("snapshot = %v", got)
	}
	if frame, err := stream.Recv(); err != nil || frame.GetSnapshotComplete() == nil {
		t.Fatalf("snapshot complete %v %v", frame, err)
	}
	if err := kube.Delete(context.Background(), binding("one", "alice", "base")); err != nil {
		t.Fatal(err)
	}
	frame, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if frame.GetRemoved().GetUid() != "uid-one" {
		t.Fatalf("removed = %v", frame)
	}
	denied, err := api.WatchPolicyBindings(tokenContext(t, "alice"), &policypb.WatchPolicyBindingsRequest{Subject: &identitypb.ServiceAccount{Namespace: "tools", Name: "bob"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = denied.Recv(); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("cross-subject read: %v", err)
	}
}

func TestSetWatchRemovesSetsWhenBindingIsRevoked(t *testing.T) {
	otherNamespace := policySet("base")
	otherNamespace.Namespace = "other-policies"
	api, kube, _ := fixture(t, binding("one", "alice", "base"), policySet("base"), policySet("private"), otherNamespace)
	stream, err := api.WatchPolicySets(tokenContext(t, "alice"), &policypb.WatchPolicySetsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if got := first.GetSnapshot().GetPolicySets(); len(got) != 1 || got[0].GetIdentity().GetNamespace() != "policies" || got[0].GetIdentity().GetName() != "base" {
		t.Fatalf("snapshot = %v", got)
	}
	if frame, err := stream.Recv(); err != nil || frame.GetSnapshotComplete() == nil {
		t.Fatalf("snapshot complete %v %v", frame, err)
	}
	if err := kube.Delete(context.Background(), binding("one", "alice", "base")); err != nil {
		t.Fatal(err)
	}
	frame, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if frame.GetRemoved().GetUid() != "uid-base" {
		t.Fatalf("removed = %v", frame)
	}
}

func TestListPolicySetNamesReturnsOneSortedCallerVisibleSnapshot(t *testing.T) {
	otherNamespace := policySet("other-namespace")
	otherNamespace.Namespace = "other-policies"
	api, _, _ := fixture(
		t,
		binding("one", "alice", "base"), policySet("z-private"), policySet("base"), policySet("alpha"), otherNamespace,
	)

	for _, test := range []struct {
		token string
		want  []string
	}{
		{token: "app", want: []string{"alpha", "base", "z-private"}},
		{token: "alice", want: []string{"base"}},
	} {
		response, err := api.ListPolicySetNames(tokenContext(t, test.token), &emptypb.Empty{})
		if err != nil {
			t.Fatalf("ListPolicySetNames(%s): %v", test.token, err)
		}
		if !reflect.DeepEqual(response.GetNames(), test.want) {
			t.Errorf("ListPolicySetNames(%s) = %v, want %v", test.token, response.GetNames(), test.want)
		}
	}
}
