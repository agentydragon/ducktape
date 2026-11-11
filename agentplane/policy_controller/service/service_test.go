package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
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
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true, Audiences: review.Spec.Audiences,
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:tools:" + name, UID: "uid-" + name,
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"policy-test-" + name},
				"authentication.kubernetes.io/pod-uid":  {"pod-uid-" + name},
			},
		},
	}}, nil
}

type serviceTestClient struct {
	client.WithWatch
	created             int
	deletePreconditions *metav1.Preconditions
	forceDeleteConflict bool
	beforeCreate        func() error
}

func (c *serviceTestClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	if c.beforeCreate != nil {
		if err := c.beforeCreate(); err != nil {
			return err
		}
		c.beforeCreate = nil
	}
	if object.GetGenerateName() != "" {
		c.created++
		object.SetName(fmt.Sprintf("%s%05d", object.GetGenerateName(), c.created))
		object.SetUID(types.UID(fmt.Sprintf("generated-%d", c.created)))
	}
	return c.WithWatch.Create(ctx, object, options...)
}

func (c *serviceTestClient) Delete(ctx context.Context, object client.Object, options ...client.DeleteOption) error {
	var deleteOptions client.DeleteOptions
	for _, option := range options {
		option.ApplyToDelete(&deleteOptions)
	}
	if deleteOptions.Preconditions != nil {
		copy := *deleteOptions.Preconditions
		c.deletePreconditions = &copy
	}
	if c.forceDeleteConflict {
		return apierrors.NewConflict(schema.GroupResource{Group: policyapi.SchemeGroupVersion.Group, Resource: "policybindings"}, object.GetName(), errors.New("test resourceVersion conflict"))
	}
	return c.WithWatch.Delete(ctx, object, options...)
}

func fixture(t *testing.T, objects ...client.Object) (policypb.PolicyServiceClient, *serviceTestClient, *reviews) {
	return fixtureWithWriters(t, []auth.Account{{Namespace: "tools", Name: "app"}}, objects...)
}

func fixtureWithWriters(t *testing.T, writers []auth.Account, objects ...client.Object) (policypb.PolicyServiceClient, *serviceTestClient, *reviews) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := workloadapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kube := &serviceTestClient{WithWatch: fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()}
	readers := []auth.Account{{Namespace: "tools", Name: "app"}}
	service, err := New("policies", []string{"policies", "fake-workloads"}, kube, readers, writers)
	if err != nil {
		t.Fatal(err)
	}
	reviews := &reviews{}
	readResolver, err := auth.NewResolver(reviews, "policy", []string{"tools"}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	writeResolver, err := auth.NewResolver(reviews, "policy", []string{"tools"}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(service.Methods(readResolver, writeResolver).ServerOptions(time.Second)...)
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
	return &policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: name, UID: types.UID("uid-" + name), ResourceVersion: "1", Generation: 1}, Spec: policyapi.PolicyBindingSpec{Subject: policyapi.Subject{Namespace: "tools", Name: subject}, PolicySets: sets}}
}

func testSandbox(name, uid string) *sandboxapi.Sandbox {
	return &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: name, UID: types.UID(uid)}}
}

func testExecutionAccount(name, sandboxUID, accountUID string) *corev1.ServiceAccount {
	controller := true
	return &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: "policies", Name: name, UID: types.UID(accountUID),
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
			Name: name, UID: types.UID(sandboxUID), Controller: &controller,
		}},
	}}
}

func policySet(name string) *policyapi.PolicySet {
	return &policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: name, UID: types.UID("uid-" + name), Generation: 1}, Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: "action_auto_approve", Policy: &policyapi.ActionPolicy{Type: "exact_actions", Actions: map[string]policyapi.ActionNames{"group": {"inspect", "write"}}}}}}}
}

func TestPolicyWritesAreRestrictedToTheConfiguredWriterAccount(t *testing.T) {
	api, _, _ := fixture(
		t,
		testSandbox("worker", "sandbox-uid"), testExecutionAccount("worker", "sandbox-uid", "account-uid"), policySet("base"),
	)
	grant := &policypb.GrantSandboxPolicyBindingRequest{SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", PolicySets: []string{"base"}}
	if _, err := api.GrantSandboxPolicyBinding(tokenContext(t, "alice"), grant); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("non-writer grant: %v", err)
	}
	revoke := &policypb.RevokeSandboxPolicyBindingRequest{SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", BindingName: "grant", BindingUid: "binding-uid"}
	if _, err := api.RevokeSandboxPolicyBinding(tokenContext(t, "alice"), revoke); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("non-writer revoke: %v", err)
	}
}

func TestEmptyWriterAllowlistDeniesPolicyWrites(t *testing.T) {
	api, _, _ := fixtureWithWriters(
		t, nil,
		testSandbox("worker", "sandbox-uid"), testExecutionAccount("worker", "sandbox-uid", "account-uid"), policySet("base"),
	)
	grant := &policypb.GrantSandboxPolicyBindingRequest{SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", PolicySets: []string{"base"}}
	if _, err := api.GrantSandboxPolicyBinding(tokenContext(t, "app"), grant); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("grant with empty writer allowlist: %v", err)
	}
	revoke := &policypb.RevokeSandboxPolicyBindingRequest{SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", BindingName: "grant", BindingUid: "binding-uid"}
	if _, err := api.RevokeSandboxPolicyBinding(tokenContext(t, "app"), revoke); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("revoke with empty writer allowlist: %v", err)
	}
}

func TestGrantCreatesBindingForExactSandboxIncarnation(t *testing.T) {
	api, kube, _ := fixture(
		t,
		testSandbox("worker", "sandbox-uid"), testExecutionAccount("worker", "sandbox-uid", "account-uid"), policySet("base"),
	)
	expiresAt := timestamppb.New(time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC))
	created, err := api.GrantSandboxPolicyBinding(tokenContext(t, "app"), &policypb.GrantSandboxPolicyBindingRequest{
		SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", PolicySets: []string{"base"}, ExpiresAt: expiresAt,
	})
	if err != nil {
		t.Fatal(err)
	}
	binding := created.GetBinding()
	if binding.GetIdentity().GetNamespace() != "policies" || binding.GetIdentity().GetName() != "worker-00001" || binding.GetIdentity().GetUid() != "generated-1" {
		t.Fatalf("created identity = %v", binding.GetIdentity())
	}
	if binding.GetSpec().GetSubject().GetNamespace() != "policies" || binding.GetSpec().GetSubject().GetName() != "worker" ||
		len(binding.GetSpec().GetPolicySets()) != 1 || binding.GetSpec().GetPolicySets()[0] != "base" ||
		binding.GetSpec().GetExpiresAt() == nil || !binding.GetSpec().GetExpiresAt().AsTime().Equal(expiresAt.AsTime()) {
		t.Fatalf("created binding spec = %v", binding.GetSpec())
	}
	owners := binding.GetOwnerReferences()
	if len(owners) != 1 || owners[0].GetApiVersion() != sandboxapi.GroupVersion.String() || owners[0].GetKind() != sandboxapi.SandboxKind ||
		owners[0].GetName() != "worker" || owners[0].GetUid() != "sandbox-uid" || owners[0].GetController() {
		t.Fatalf("created binding owners = %v", owners)
	}
	stored := &policyapi.PolicyBinding{}
	if err := kube.Get(context.Background(), client.ObjectKey{Namespace: "policies", Name: "worker-00001"}, stored); err != nil {
		t.Fatal(err)
	}
}

func TestGrantRejectsStaleSandboxAndInvalidPolicySets(t *testing.T) {
	api, kube, _ := fixture(
		t,
		testSandbox("worker", "sandbox-current"), testExecutionAccount("worker", "sandbox-current", "account-uid"), policySet("base"),
	)
	call := func(uid string, sets ...string) error {
		_, err := api.GrantSandboxPolicyBinding(tokenContext(t, "app"), &policypb.GrantSandboxPolicyBindingRequest{
			SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: uid, PolicySets: sets,
		})
		return err
	}
	if err := call("sandbox-replaced", "base"); status.Code(err) != codes.NotFound {
		t.Fatalf("stale Sandbox UID grant: %v", err)
	}
	for _, sets := range [][]string{nil, {"missing"}, {"base", "base"}, {""}} {
		if err := call("sandbox-current", sets...); status.Code(err) != codes.InvalidArgument {
			t.Errorf("grant with PolicySets %q: %v", sets, err)
		}
	}
	var bindings policyapi.PolicyBindingList
	if err := kube.List(context.Background(), &bindings, client.InNamespace("policies")); err != nil {
		t.Fatal(err)
	}
	if len(bindings.Items) != 0 {
		t.Fatalf("invalid grants created bindings: %v", bindings.Items)
	}
}

func TestGrantDetectsSandboxReplacementAfterCreateAndCleansUp(t *testing.T) {
	api, kube, _ := fixture(
		t,
		testSandbox("worker", "sandbox-old"), testExecutionAccount("worker", "sandbox-old", "account-old"), policySet("base"),
	)
	kube.beforeCreate = func() error {
		if err := kube.WithWatch.Delete(context.Background(), testSandbox("worker", "sandbox-old")); err != nil {
			return err
		}
		if err := kube.WithWatch.Delete(context.Background(), testExecutionAccount("worker", "sandbox-old", "account-old")); err != nil {
			return err
		}
		if err := kube.WithWatch.Create(context.Background(), testSandbox("worker", "sandbox-new")); err != nil {
			return err
		}
		return kube.WithWatch.Create(context.Background(), testExecutionAccount("worker", "sandbox-new", "account-new"))
	}
	_, err := api.GrantSandboxPolicyBinding(tokenContext(t, "app"), &policypb.GrantSandboxPolicyBindingRequest{
		SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-old", PolicySets: []string{"base"},
	})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("grant across Sandbox replacement: %v", err)
	}
	var bindings policyapi.PolicyBindingList
	if err := kube.List(context.Background(), &bindings, client.InNamespace("policies")); err != nil {
		t.Fatal(err)
	}
	if len(bindings.Items) != 0 {
		t.Fatalf("stale binding survived post-create cleanup: %v", bindings.Items)
	}
	if kube.deletePreconditions == nil || kube.deletePreconditions.UID == nil || *kube.deletePreconditions.UID != "generated-1" ||
		kube.deletePreconditions.ResourceVersion == nil || *kube.deletePreconditions.ResourceVersion != "1" {
		t.Fatalf("stale-binding cleanup preconditions = %+v", kube.deletePreconditions)
	}
}

func operatorBinding(sandboxUID string) *policyapi.PolicyBinding {
	operator := binding("operator-grant", "worker", "base")
	operator.Spec.Subject = policyapi.Subject{Namespace: "policies", Name: "worker"}
	controller, blockOwnerDeletion := false, false
	operator.OwnerReferences = []metav1.OwnerReference{{
		APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: "worker", UID: types.UID(sandboxUID),
		Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
	}}
	return operator
}

func TestRevokeSandboxBindingUsesUIDResourceVersionPreconditions(t *testing.T) {
	api, kube, _ := fixture(
		t,
		testSandbox("worker", "sandbox-uid"), testExecutionAccount("worker", "sandbox-uid", "account-uid"), operatorBinding("sandbox-uid"),
	)
	_, err := api.RevokeSandboxPolicyBinding(tokenContext(t, "app"), &policypb.RevokeSandboxPolicyBindingRequest{
		SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", BindingName: "operator-grant", BindingUid: "uid-operator-grant",
	})
	if err != nil {
		t.Fatal(err)
	}
	if kube.deletePreconditions == nil || kube.deletePreconditions.UID == nil || *kube.deletePreconditions.UID != "uid-operator-grant" ||
		kube.deletePreconditions.ResourceVersion == nil || *kube.deletePreconditions.ResourceVersion != "1" {
		t.Fatalf("revoke preconditions = %+v", kube.deletePreconditions)
	}
	if err := kube.Get(context.Background(), client.ObjectKey{Namespace: "policies", Name: "operator-grant"}, &policyapi.PolicyBinding{}); !apierrors.IsNotFound(err) {
		t.Fatalf("revoked PolicyBinding remains: %v", err)
	}
}

func TestRevokeRejectsStaleOrOwnedBindingsAndResourceVersionConflicts(t *testing.T) {
	controller := true
	for _, test := range []struct {
		name       string
		binding    func() *policyapi.PolicyBinding
		bindingUID string
		conflict   bool
	}{
		{name: "stale binding uid", binding: func() *policyapi.PolicyBinding { return operatorBinding("sandbox-uid") }, bindingUID: "another-binding-uid"},
		{name: "Flux owned", binding: func() *policyapi.PolicyBinding {
			item := operatorBinding("sandbox-uid")
			item.Labels = map[string]string{"kustomize.toolkit.fluxcd.io/name": "agentplane-staging"}
			return item
		}, bindingUID: "uid-operator-grant"},
		{name: "Sandbox controller owned", binding: func() *policyapi.PolicyBinding {
			item := operatorBinding("sandbox-uid")
			item.OwnerReferences[0].Controller = &controller
			return item
		}, bindingUID: "uid-operator-grant"},
		{name: "another Sandbox incarnation", binding: func() *policyapi.PolicyBinding { return operatorBinding("sandbox-old") }, bindingUID: "uid-operator-grant"},
		{name: "another ServiceAccount", binding: func() *policyapi.PolicyBinding {
			item := operatorBinding("sandbox-uid")
			item.Spec.Subject.Name = "other"
			return item
		}, bindingUID: "uid-operator-grant"},
		{name: "resourceVersion changed during delete", binding: func() *policyapi.PolicyBinding { return operatorBinding("sandbox-uid") }, bindingUID: "uid-operator-grant", conflict: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			api, kube, _ := fixture(
				t,
				testSandbox("worker", "sandbox-uid"), testExecutionAccount("worker", "sandbox-uid", "account-uid"), test.binding(),
			)
			kube.forceDeleteConflict = test.conflict
			_, err := api.RevokeSandboxPolicyBinding(tokenContext(t, "app"), &policypb.RevokeSandboxPolicyBindingRequest{
				SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", BindingName: "operator-grant", BindingUid: test.bindingUID,
			})
			if status.Code(err) != codes.Aborted {
				t.Fatalf("revoke result = %v", err)
			}
			if err := kube.Get(context.Background(), client.ObjectKey{Namespace: "policies", Name: "operator-grant"}, &policyapi.PolicyBinding{}); err != nil {
				t.Fatalf("refused binding was deleted: %v", err)
			}
		})
	}
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
	denied, err := api.WatchPolicyBindings(tokenContext(t, "alice"), &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_Subject{Subject: &identitypb.ServiceAccount{Namespace: "tools", Name: "bob"}}})
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
		response, err := api.ListPolicySetNames(tokenContext(t, test.token), &policypb.ListPolicySetNamesRequest{})
		if err != nil {
			t.Fatalf("ListPolicySetNames(%s): %v", test.token, err)
		}
		if !reflect.DeepEqual(response.GetNames(), test.want) {
			t.Errorf("ListPolicySetNames(%s) = %v, want %v", test.token, response.GetNames(), test.want)
		}
	}
}

func testIdentity(name string, labels map[string]string) *policyapi.Identity {
	return &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: name, UID: types.UID("uid-" + name), ResourceVersion: "1", Generation: 1, Labels: labels},
		Spec:       policyapi.IdentitySpec{Description: "Fake agent " + name},
	}
}

func identityBinding(name, identity string, owner *policyapi.Identity) *policyapi.PolicyBinding {
	bound := binding(name, "", "base")
	bound.Spec.Subject = policyapi.Subject{Identity: &policyapi.IdentityRef{Name: identity}}
	if owner != nil {
		bound.OwnerReferences = []metav1.OwnerReference{{APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "Identity", Name: owner.Name, UID: owner.UID}}
	}
	return bound
}

func identityCredential(namespace, name string, identity *policyapi.Identity) *workloadapi.WorkloadCredential {
	return &workloadapi.WorkloadCredential{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name},
		Spec: workloadapi.WorkloadCredentialSpec{
			Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: identity.Name, UID: identity.UID}},
		},
	}
}

func TestIdentityBindingWatchesSelectOnlyIdentitySubjects(t *testing.T) {
	api, _, _ := fixture(t, identityBinding("names-agent", "fake-agent", nil), identityBinding("names-other", "other-agent", nil), binding("account", "fake-agent", "base"))
	byName := &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_Identity{Identity: "fake-agent"}}
	every := &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_EveryIdentity{EveryIdentity: true}}
	for _, tc := range []struct {
		request *policypb.WatchPolicyBindingsRequest
		want    []string
	}{
		{byName, []string{"names-agent"}},
		{every, []string{"names-agent", "names-other"}},
	} {
		stream, err := api.WatchPolicyBindings(tokenContext(t, "app"), tc.request)
		if err != nil {
			t.Fatal(err)
		}
		first, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		var got []string
		for _, item := range first.GetSnapshot().GetBindings() {
			got = append(got, item.GetIdentity().GetName())
		}
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%v snapshot = %v, want %v", tc.request, got, tc.want)
		}
	}
	for _, tc := range []struct {
		token   string
		request *policypb.WatchPolicyBindingsRequest
		want    codes.Code
	}{
		{"alice", byName, codes.PermissionDenied},
		{"alice", every, codes.PermissionDenied},
		{"app", &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_EveryIdentity{}}, codes.InvalidArgument},
		{"app", &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_Identity{}}, codes.InvalidArgument},
	} {
		refused, err := api.WatchPolicyBindings(tokenContext(t, tc.token), tc.request)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := refused.Recv(); status.Code(err) != tc.want {
			t.Errorf("%s watching %v: %v, want %v", tc.token, tc.request, err, tc.want)
		}
	}
}

func TestIdentityWatchFollowsIdentitiesForBroadReadersOnly(t *testing.T) {
	flux := map[string]string{"kustomize.toolkit.fluxcd.io/name": "fake-kustomization"}
	api, kube, _ := fixture(t, testIdentity("git-origin-identity", flux))
	stream, err := api.WatchIdentities(tokenContext(t, "app"), &policypb.WatchIdentitiesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	got := first.GetSnapshot().GetIdentities()
	if len(got) != 1 || got[0].GetIdentity().GetName() != "git-origin-identity" || got[0].GetSpec().GetDescription() != "Fake agent git-origin-identity" ||
		got[0].GetLabels()["kustomize.toolkit.fluxcd.io/name"] != "fake-kustomization" {
		t.Fatalf("snapshot = %v", got)
	}
	if frame, err := stream.Recv(); err != nil || frame.GetSnapshotComplete() == nil {
		t.Fatalf("snapshot complete %v %v", frame, err)
	}
	created := testIdentity("app-identity", nil)
	created.ResourceVersion = ""
	if err := kube.Create(context.Background(), created); err != nil {
		t.Fatal(err)
	}
	if frame, err := stream.Recv(); err != nil || frame.GetUpsert().GetIdentity().GetName() != "app-identity" {
		t.Fatalf("upsert = %v %v", frame, err)
	}
	if err := kube.Delete(context.Background(), testIdentity("git-origin-identity", nil)); err != nil {
		t.Fatal(err)
	}
	if frame, err := stream.Recv(); err != nil || frame.GetRemoved().GetUid() != "uid-git-origin-identity" {
		t.Fatalf("removed = %v %v", frame, err)
	}
	denied, err := api.WatchIdentities(tokenContext(t, "alice"), &policypb.WatchIdentitiesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := denied.Recv(); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("narrow reader watching identities: %v", err)
	}
}

func TestDeleteIdentityRemovesAnUnreferencedIdentityExactly(t *testing.T) {
	identity := testIdentity("unreferenced-identity", nil)
	api, kube, _ := fixture(t, identity, identityBinding("own-launch-binding", "unreferenced-identity", identity))
	_, err := api.DeleteIdentity(tokenContext(t, "app"), &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "unreferenced-identity", Uid: "uid-unreferenced-identity"})
	if err != nil {
		t.Fatal(err)
	}
	if kube.deletePreconditions == nil || kube.deletePreconditions.UID == nil || *kube.deletePreconditions.UID != "uid-unreferenced-identity" ||
		kube.deletePreconditions.ResourceVersion == nil || *kube.deletePreconditions.ResourceVersion != "1" {
		t.Fatalf("delete preconditions = %+v", kube.deletePreconditions)
	}
	if err := kube.Get(context.Background(), client.ObjectKey{Namespace: "policies", Name: "unreferenced-identity"}, &policyapi.Identity{}); !apierrors.IsNotFound(err) {
		t.Fatalf("deleted Identity remains: %v", err)
	}
}

func TestDeleteIdentityIgnoresCredentialsActingAsAnotherIdentity(t *testing.T) {
	identity := testIdentity("unreferenced-identity", nil)
	previous := testIdentity("unreferenced-identity", nil)
	previous.UID = "uid-previous-incarnation"
	api, kube, _ := fixture(
		t, identity,
		identityCredential("fake-workloads", "other-agent", testIdentity("other-identity", nil)),
		identityCredential("fake-workloads", "previous-agent", previous),
		&workloadapi.WorkloadCredential{
			ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: "account-agent"},
			Spec:       workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{ServiceAccount: "unreferenced-identity"}},
		},
	)
	if _, err := api.DeleteIdentity(tokenContext(t, "app"), &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "unreferenced-identity", Uid: "uid-unreferenced-identity"}); err != nil {
		t.Fatal(err)
	}
	if err := kube.Get(context.Background(), client.ObjectKey{Namespace: "policies", Name: "unreferenced-identity"}, &policyapi.Identity{}); !apierrors.IsNotFound(err) {
		t.Fatalf("deleted Identity remains: %v", err)
	}
}

func refusalViolations(err error) []string {
	var named []string
	for _, detail := range status.Convert(err).Details() {
		if failure, ok := detail.(*errdetails.PreconditionFailure); ok {
			for _, violation := range failure.GetViolations() {
				named = append(named, violation.GetType()+"/"+violation.GetSubject())
			}
		}
	}
	return named
}

func TestDeleteIdentityRefusesWhileAWorkloadCredentialActsAsIt(t *testing.T) {
	credentialed := testIdentity("credentialed-identity", nil)
	both := testIdentity("doubly-referenced-identity", nil)
	api, kube, _ := fixture(
		t, credentialed, both,
		identityCredential("fake-workloads", "standing-agent", credentialed),
		identityCredential("policies", "sandbox-agent", credentialed),
		identityCredential("fake-workloads", "standing-agent-2", both),
		identityBinding("standing-grant", "doubly-referenced-identity", nil),
	)
	for _, tc := range []struct {
		identity *policyapi.Identity
		want     []string
	}{
		{credentialed, []string{"WorkloadCredential/fake-workloads/standing-agent", "WorkloadCredential/policies/sandbox-agent"}},
		{both, []string{"PolicyBinding/standing-grant", "WorkloadCredential/fake-workloads/standing-agent-2"}},
	} {
		_, err := api.DeleteIdentity(tokenContext(t, "app"), &policypb.DeleteIdentityRequest{Namespace: "policies", Name: tc.identity.Name, Uid: string(tc.identity.UID)})
		if status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("%s: %v, want FailedPrecondition", tc.identity.Name, err)
		}
		if got := refusalViolations(err); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s refusal names %v, want %v", tc.identity.Name, got, tc.want)
		}
	}
	var identities policyapi.IdentityList
	if err := kube.List(context.Background(), &identities, client.InNamespace("policies")); err != nil {
		t.Fatal(err)
	}
	if len(identities.Items) != 2 {
		t.Fatalf("refused deletes removed identities: %v", identities.Items)
	}
}

func TestDeleteIdentityRefusesWhatItMustNotRemove(t *testing.T) {
	referenced := testIdentity("referenced-identity", nil)
	// A binding owned by another incarnation of the same name is not this identity's own.
	previous := testIdentity("referenced-identity", nil)
	previous.UID = "uid-previous-incarnation"
	api, kube, _ := fixture(
		t,
		referenced, identityBinding("standing-grant", "referenced-identity", nil), identityBinding("stale-owner", "referenced-identity", previous),
		testIdentity("git-origin-identity", map[string]string{"kustomize.toolkit.fluxcd.io/name": "fake-kustomization"}),
	)
	for _, tc := range []struct {
		name    string
		token   string
		request *policypb.DeleteIdentityRequest
		want    codes.Code
	}{
		{"referenced", "app", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "referenced-identity", Uid: "uid-referenced-identity"}, codes.FailedPrecondition},
		{"git origin", "app", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "git-origin-identity", Uid: "uid-git-origin-identity"}, codes.FailedPrecondition},
		{"replaced incarnation", "app", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "git-origin-identity", Uid: "uid-replaced"}, codes.Aborted},
		{"another namespace", "app", &policypb.DeleteIdentityRequest{Namespace: "other-policies", Name: "referenced-identity", Uid: "uid-referenced-identity"}, codes.NotFound},
		{"missing UID", "app", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "referenced-identity"}, codes.InvalidArgument},
		{"non-writer", "alice", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "referenced-identity", Uid: "uid-referenced-identity"}, codes.PermissionDenied},
	} {
		_, err := api.DeleteIdentity(tokenContext(t, tc.token), tc.request)
		if status.Code(err) != tc.want {
			t.Errorf("%s: %v, want %v", tc.name, err, tc.want)
		}
		if tc.name == "referenced" {
			if named, want := refusalViolations(err), []string{"PolicyBinding/stale-owner", "PolicyBinding/standing-grant"}; !reflect.DeepEqual(named, want) {
				t.Errorf("referenced refusal names %v, want %v", named, want)
			}
		}
	}
	var identities policyapi.IdentityList
	if err := kube.List(context.Background(), &identities, client.InNamespace("policies")); err != nil {
		t.Fatal(err)
	}
	if len(identities.Items) != 2 {
		t.Fatalf("refused deletes removed identities: %v", identities.Items)
	}
}

func TestCreateIdentityCreatesAnUnownedIdentityOnce(t *testing.T) {
	api, kube, _ := fixture(t)
	created, err := api.CreateIdentity(tokenContext(t, "app"), &policypb.CreateIdentityRequest{Name: "launch-a", Description: "Agent of launch a"})
	if err != nil {
		t.Fatal(err)
	}
	if created.GetIdentity().GetIdentity().GetName() != "launch-a" || created.GetIdentity().GetSpec().GetDescription() != "Agent of launch a" {
		t.Fatalf("created = %v", created)
	}
	var stored policyapi.Identity
	if err := kube.Get(context.Background(), client.ObjectKey{Namespace: "policies", Name: "launch-a"}, &stored); err != nil {
		t.Fatal(err)
	}
	if len(stored.OwnerReferences) != 0 {
		t.Fatalf("Identity owners = %v, want none", stored.OwnerReferences)
	}
	if _, err := api.CreateIdentity(tokenContext(t, "app"), &policypb.CreateIdentityRequest{Name: "launch-a", Description: "again"}); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("second create: %v", err)
	}
	if _, err := api.CreateIdentity(tokenContext(t, "app"), &policypb.CreateIdentityRequest{Name: "launch-b"}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("create without description: %v", err)
	}
	if _, err := api.CreateIdentity(tokenContext(t, "alice"), &policypb.CreateIdentityRequest{Name: "launch-c", Description: "x"}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("non-writer create: %v", err)
	}
}

func TestGrantIdentityPolicyBindingIsOwnedByTheExactIdentity(t *testing.T) {
	identity := testIdentity("launch-a", nil)
	api, kube, _ := fixture(t, identity, policySet("base"))
	granted, err := api.GrantIdentityPolicyBinding(tokenContext(t, "app"), &policypb.GrantIdentityPolicyBindingRequest{
		IdentityName: "launch-a", IdentityUid: "uid-launch-a", PolicySets: []string{"base"},
	})
	if err != nil {
		t.Fatal(err)
	}
	var stored policyapi.PolicyBinding
	if err := kube.Get(context.Background(), client.ObjectKey{Namespace: "policies", Name: granted.GetBinding().GetIdentity().GetName()}, &stored); err != nil {
		t.Fatal(err)
	}
	if stored.Spec.Subject.Identity == nil || stored.Spec.Subject.Identity.Name != "launch-a" || stored.Spec.Subject.Namespace != "" {
		t.Fatalf("subject = %+v", stored.Spec.Subject)
	}
	if !ownedBy(stored.OwnerReferences, identity.UID) {
		t.Fatalf("owners = %+v, want the Identity", stored.OwnerReferences)
	}
	// Owned, so it does not hold the Identity against deletion.
	if _, err := api.DeleteIdentity(tokenContext(t, "app"), &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "launch-a", Uid: "uid-launch-a"}); err != nil {
		t.Fatalf("delete with only owned grants: %v", err)
	}
}

func TestGrantIdentityPolicyBindingRefusesAnotherIncarnationAndUnknownSets(t *testing.T) {
	api, _, _ := fixture(t, testIdentity("launch-a", nil), policySet("base"))
	for name, request := range map[string]*policypb.GrantIdentityPolicyBindingRequest{
		"stale UID":   {IdentityName: "launch-a", IdentityUid: "uid-previous", PolicySets: []string{"base"}},
		"missing":     {IdentityName: "launch-z", IdentityUid: "uid-launch-z", PolicySets: []string{"base"}},
		"unknown set": {IdentityName: "launch-a", IdentityUid: "uid-launch-a", PolicySets: []string{"nope"}},
		"no sets":     {IdentityName: "launch-a", IdentityUid: "uid-launch-a"},
	} {
		if _, err := api.GrantIdentityPolicyBinding(tokenContext(t, "app"), request); err == nil {
			t.Errorf("%s: granted", name)
		}
	}
}
