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
	kube := &serviceTestClient{WithWatch: fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()}
	readers := []auth.Account{{Namespace: "tools", Name: "app"}}
	service, err := New("policies", kube, readers, writers)
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
		response, err := api.ListPolicySetNames(tokenContext(t, test.token), &policypb.ListPolicySetNamesRequest{})
		if err != nil {
			t.Fatalf("ListPolicySetNames(%s): %v", test.token, err)
		}
		if !reflect.DeepEqual(response.GetNames(), test.want) {
			t.Errorf("ListPolicySetNames(%s) = %v, want %v", test.token, response.GetNames(), test.want)
		}
	}
}
