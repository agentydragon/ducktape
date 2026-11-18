package rpc

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

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

type policyClients map[string]policypb.PolicyServiceClient

func fixture(t *testing.T, objects ...client.Object) (policyClients, *serviceTestClient) {
	return fixtureWithWriters(t, []string{peertls.AppURI}, objects...)
}

func fixtureWithWriters(t *testing.T, writers []string, objects ...client.Object) (policyClients, *serviceTestClient) {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, policyapi.AddToScheme, sandboxapi.AddToScheme, workloadapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	kube := &serviceTestClient{WithWatch: fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()}
	service, err := New("policies", []string{"policies", "fake-workloads"}, kube, []string{peertls.AppURI}, writers)
	if err != nil {
		t.Fatal(err)
	}
	methods, err := service.Methods()
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLSWithTimeout(t, time.Second, peertls.PolicyControllerURI, methods, func(server *grpc.Server) {
		policypb.RegisterPolicyServiceServer(server, service)
	}, peertls.AppURI, peertls.NotificationsURI)
	clients := policyClients{}
	for name, uri := range map[string]string{"app": peertls.AppURI, "unlisted": peertls.NotificationsURI} {
		clients[name] = policypb.NewPolicyServiceClient(grpcmethods.AsCaller(connection, uri))
	}
	return clients, kube
}

func TestPolicyServicePeerTLSUsesSeparateReadAndWriteAllowlists(t *testing.T) {
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, policyapi.AddToScheme, sandboxapi.AddToScheme, workloadapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	kube := &serviceTestClient{WithWatch: fake.NewClientBuilder().WithScheme(scheme).WithObjects(policySet("base")).Build()}
	service, err := New("policies", []string{"policies", "fake-workloads"}, kube,
		[]string{peertls.ActionsURI}, []string{peertls.EgressProxyURI})
	if err != nil {
		t.Fatal(err)
	}
	methods, err := service.Methods()
	if err != nil {
		t.Fatal(err)
	}

	for _, method := range grpcmethods.Of(policypb.PolicyService_ServiceDesc) {
		ctx := metadata.NewIncomingContext(t.Context(), metadata.Pairs("authorization", "Bearer test-token"))
		if _, err := methods.AdmitContext(ctx, method.Name, time.Second); status.Code(err) != codes.Unauthenticated {
			t.Errorf("bearer admission for %s: %v", method.Name, err)
		}
	}

	ca := transporttesting.NewCA(t)
	serverSVID := ca.Issue(t, peertls.PolicyControllerURI)
	serverCredentials, err := (peertls.ServerTLSListener{
		Listen: ":0", CertFile: serverSVID.CertFile, KeyFile: serverSVID.KeyFile, ClientCAFile: ca.RootFile,
	}).Credentials(peertls.PolicyControllerURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(serverCredentials))...)
	policypb.RegisterPolicyServiceServer(server, service)
	address := grpcserver.Serve(t, server)

	dial := func(t *testing.T, tlsConfig *tls.Config) policypb.PolicyServiceClient {
		t.Helper()
		connection, err := grpc.NewClient(address, grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(tlsConfig)))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = connection.Close() })
		return policypb.NewPolicyServiceClient(connection)
	}
	clientTLS := func(serviceID string) *tls.Config {
		clientSVID := ca.Issue(t, serviceID)
		config, err := clientSVID.ClientTLSConfig(peertls.PolicyControllerURI)
		if err != nil {
			t.Fatal(err)
		}
		return config
	}
	readerClient := dial(t, clientTLS(peertls.ActionsURI))
	names, err := readerClient.ListPolicySetNames(context.Background(), &policypb.ListPolicySetNamesRequest{})
	if err != nil || !slices.Equal(names.GetNames(), []string{"base"}) {
		t.Fatalf("allowlisted service reader: names=%v, error=%v", names.GetNames(), err)
	}
	if _, err := readerClient.CreateIdentity(context.Background(), &policypb.CreateIdentityRequest{Name: "service-created", Description: "test"}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("read-only service caller created an Identity: %v", err)
	}

	writerClient := dial(t, clientTLS(peertls.EgressProxyURI))
	if _, err := writerClient.CreateIdentity(context.Background(), &policypb.CreateIdentityRequest{Name: "service-created", Description: "test"}); err != nil {
		t.Fatalf("allowlisted service writer: %v", err)
	}
	if _, err := writerClient.ListPolicySetNames(context.Background(), &policypb.ListPolicySetNamesRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("write-only service caller read PolicySets: %v", err)
	}

	wrongClient := dial(t, clientTLS(peertls.NotificationsURI))
	if _, err := wrongClient.ListPolicySetNames(context.Background(), &policypb.ListPolicySetNamesRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("unlisted service caller read PolicySets: %v", err)
	}
	if _, err := readerClient.ListPolicySetNames(tokenContext(t, "app"), &policypb.ListPolicySetNamesRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("service certificate was combined with a bearer: %v", err)
	}

	bearerTLS, err := (peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.PolicyControllerURI}).TLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	bearerClient := dial(t, bearerTLS)
	if _, err := bearerClient.ListPolicySetNames(tokenContext(t, "app"), &policypb.ListPolicySetNamesRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("bearer read over TLS: %v", err)
	}

	untrustedCA := transporttesting.NewCA(t)
	untrustedSVID := untrustedCA.Issue(t, peertls.ActionsURI)
	untrustedTLS, err := untrustedSVID.ClientTLSConfig(peertls.PolicyControllerURI)
	if err != nil {
		t.Fatal(err)
	}
	untrustedClient := dial(t, untrustedTLS)
	if _, err := untrustedClient.ListPolicySetNames(context.Background(), &policypb.ListPolicySetNamesRequest{}); err == nil {
		t.Fatal("service certificate from an untrusted CA was admitted")
	}
}

func testContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	t.Cleanup(cancel)
	return ctx
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
	return &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: name, UID: types.UID(uid)},
		Spec:       sandboxapi.SandboxSpec{Identity: &sandboxapi.IdentityRef{Name: "agent-" + name}},
	}
}

func policySet(name string) *policyapi.PolicySet {
	return &policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: name, UID: types.UID("uid-" + name), Generation: 1}, Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: "action_auto_approve", Policy: &policyapi.ActionPolicy{Type: "exact_actions", Actions: map[string]policyapi.ActionNames{"group": {"inspect", "write"}}}}}}}
}

func TestPolicyWritesAreRestrictedToTheConfiguredWriterService(t *testing.T) {
	api, _ := fixture(
		t,
		testSandbox("worker", "sandbox-uid"), testIdentity("agent-worker", nil), policySet("base"),
	)
	grant := &policypb.GrantSandboxPolicyBindingRequest{SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", PolicySets: []string{"base"}}
	if _, err := api["unlisted"].GrantSandboxPolicyBinding(testContext(t), grant); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("non-writer grant: %v", err)
	}
	revoke := &policypb.RevokeSandboxPolicyBindingRequest{SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", BindingName: "grant", BindingUid: "binding-uid"}
	if _, err := api["unlisted"].RevokeSandboxPolicyBinding(testContext(t), revoke); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("non-writer revoke: %v", err)
	}
}

func TestEmptyWriterAllowlistDeniesPolicyWrites(t *testing.T) {
	api, _ := fixtureWithWriters(
		t, nil,
		testSandbox("worker", "sandbox-uid"), testIdentity("agent-worker", nil), policySet("base"),
	)
	grant := &policypb.GrantSandboxPolicyBindingRequest{SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", PolicySets: []string{"base"}}
	if _, err := api["app"].GrantSandboxPolicyBinding(testContext(t), grant); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("grant with empty writer allowlist: %v", err)
	}
	revoke := &policypb.RevokeSandboxPolicyBindingRequest{SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", BindingName: "grant", BindingUid: "binding-uid"}
	if _, err := api["app"].RevokeSandboxPolicyBinding(testContext(t), revoke); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("revoke with empty writer allowlist: %v", err)
	}
}

func TestGrantCreatesBindingForExactSandboxIncarnation(t *testing.T) {
	api, kube := fixture(
		t,
		testSandbox("worker", "sandbox-uid"), testIdentity("agent-worker", nil), policySet("base"),
	)
	expiresAt := timestamppb.New(time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC))
	created, err := api["app"].GrantSandboxPolicyBinding(testContext(t), &policypb.GrantSandboxPolicyBindingRequest{
		SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: "sandbox-uid", PolicySets: []string{"base"}, ExpiresAt: expiresAt,
	})
	if err != nil {
		t.Fatal(err)
	}
	binding := created.GetBinding()
	if binding.GetIdentity().GetNamespace() != "policies" || binding.GetIdentity().GetName() != "worker-00001" || binding.GetIdentity().GetUid() != "generated-1" {
		t.Fatalf("created identity = %v", binding.GetIdentity())
	}
	if binding.GetSpec().GetSubject().GetIdentity().GetName() != "agent-worker" || binding.GetSpec().GetSubject().GetNamespace() != "" ||
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
	api, kube := fixture(
		t,
		testSandbox("worker", "sandbox-current"), testIdentity("agent-worker", nil), policySet("base"),
	)
	call := func(uid string, sets ...string) error {
		_, err := api["app"].GrantSandboxPolicyBinding(testContext(t), &policypb.GrantSandboxPolicyBindingRequest{
			SandboxNamespace: "policies", SandboxName: "worker", SandboxUid: uid, PolicySets: sets,
		})
		return err
	}
	if err := call("sandbox-replaced", "base"); status.Code(err) != codes.NotFound {
		t.Fatalf("stale Sandbox UID grant: %v", err)
	}
	if _, err := api["app"].GrantSandboxPolicyBinding(testContext(t), &policypb.GrantSandboxPolicyBindingRequest{
		SandboxNamespace: "policies", SandboxName: "Upper", SandboxUid: "sandbox-current", PolicySets: []string{"base"},
	}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("grant with invalid Sandbox name: %v", err)
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
	api, kube := fixture(
		t,
		testSandbox("worker", "sandbox-old"), testIdentity("agent-worker", nil), policySet("base"),
	)
	kube.beforeCreate = func() error {
		if err := kube.WithWatch.Delete(context.Background(), testSandbox("worker", "sandbox-old")); err != nil {
			return err
		}
		if err := kube.WithWatch.Delete(context.Background(), testIdentity("agent-worker", nil)); err != nil {
			return err
		}
		if err := kube.WithWatch.Create(context.Background(), testSandbox("worker", "sandbox-new")); err != nil {
			return err
		}
		successor := testIdentity("agent-worker", nil)
		successor.UID, successor.ResourceVersion = "uid-agent-worker-successor", ""
		return kube.WithWatch.Create(context.Background(), successor)
	}
	_, err := api["app"].GrantSandboxPolicyBinding(testContext(t), &policypb.GrantSandboxPolicyBindingRequest{
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
	operator.Spec.Subject = policyapi.Subject{Identity: &policyapi.IdentityRef{Name: "agent-worker"}}
	controller, blockOwnerDeletion := false, false
	operator.OwnerReferences = []metav1.OwnerReference{{
		APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: "worker", UID: types.UID(sandboxUID),
		Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
	}}
	return operator
}

func TestRevokeSandboxBindingUsesUIDResourceVersionPreconditions(t *testing.T) {
	api, kube := fixture(
		t,
		testSandbox("worker", "sandbox-uid"), testIdentity("agent-worker", nil), operatorBinding("sandbox-uid"),
	)
	_, err := api["app"].RevokeSandboxPolicyBinding(testContext(t), &policypb.RevokeSandboxPolicyBindingRequest{
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
			item.Labels = map[string]string{policyapi.FluxKustomizationNameLabel: "agentplane-staging"}
			return item
		}, bindingUID: "uid-operator-grant"},
		{name: "Sandbox controller owned", binding: func() *policyapi.PolicyBinding {
			item := operatorBinding("sandbox-uid")
			item.OwnerReferences[0].Controller = &controller
			return item
		}, bindingUID: "uid-operator-grant"},
		{name: "another Sandbox incarnation", binding: func() *policyapi.PolicyBinding { return operatorBinding("sandbox-old") }, bindingUID: "uid-operator-grant"},
		{name: "another Identity", binding: func() *policyapi.PolicyBinding {
			item := operatorBinding("sandbox-uid")
			item.Spec.Subject.Identity.Name = "other"
			return item
		}, bindingUID: "uid-operator-grant"},
		{name: "resourceVersion changed during delete", binding: func() *policyapi.PolicyBinding { return operatorBinding("sandbox-uid") }, bindingUID: "uid-operator-grant", conflict: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			api, kube := fixture(
				t,
				testSandbox("worker", "sandbox-uid"), testIdentity("agent-worker", nil), test.binding(),
			)
			kube.forceDeleteConflict = test.conflict
			_, err := api["app"].RevokeSandboxPolicyBinding(testContext(t), &policypb.RevokeSandboxPolicyBindingRequest{
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

func TestBindingWatchScopeAndChanges(t *testing.T) {
	otherNamespace := binding("other-namespace", "alice", "base")
	otherNamespace.Namespace = "other-policies"
	api, kube := fixture(t, binding("one", "alice", "base"), binding("foreign", "bob", "private"), otherNamespace)
	stream, err := api["app"].WatchPolicyBindings(testContext(t), &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_Subject{Subject: &identitypb.ServiceAccount{Namespace: "tools", Name: "alice"}}})
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
	denied, err := api["unlisted"].WatchPolicyBindings(testContext(t), &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_Subject{Subject: &identitypb.ServiceAccount{Namespace: "tools", Name: "bob"}}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = denied.Recv(); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("cross-subject read: %v", err)
	}
}

func TestSetWatchFollowsPolicySetRemoval(t *testing.T) {
	otherNamespace := policySet("base")
	otherNamespace.Namespace = "other-policies"
	api, kube := fixture(t, policySet("base"), otherNamespace)
	stream, err := api["app"].WatchPolicySets(testContext(t), &policypb.WatchPolicySetsRequest{})
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
	if err := kube.Delete(context.Background(), policySet("base")); err != nil {
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
	api, _ := fixture(
		t,
		binding("one", "alice", "base"), policySet("z-private"), policySet("base"), policySet("alpha"), otherNamespace,
	)

	response, err := api["app"].ListPolicySetNames(testContext(t), &policypb.ListPolicySetNamesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"alpha", "base", "z-private"}; !slices.Equal(response.GetNames(), want) {
		t.Errorf("names = %v, want %v", response.GetNames(), want)
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
	api, _ := fixture(t, identityBinding("names-agent", "fake-agent", nil), identityBinding("names-other", "other-agent", nil), binding("account", "fake-agent", "base"))
	byName := &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_Identity{Identity: "fake-agent"}}
	every := &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_EveryIdentity{EveryIdentity: true}}
	for _, tc := range []struct {
		request *policypb.WatchPolicyBindingsRequest
		want    []string
	}{
		{byName, []string{"names-agent"}},
		{every, []string{"names-agent", "names-other"}},
	} {
		stream, err := api["app"].WatchPolicyBindings(testContext(t), tc.request)
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
		caller  string
		request *policypb.WatchPolicyBindingsRequest
		want    codes.Code
	}{
		{"unlisted", byName, codes.PermissionDenied},
		{"unlisted", every, codes.PermissionDenied},
		{"app", &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_EveryIdentity{}}, codes.InvalidArgument},
		{"app", &policypb.WatchPolicyBindingsRequest{Selector: &policypb.WatchPolicyBindingsRequest_Identity{}}, codes.InvalidArgument},
		{"app", &policypb.WatchPolicyBindingsRequest{}, codes.InvalidArgument},
	} {
		refused, err := api[tc.caller].WatchPolicyBindings(testContext(t), tc.request)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := refused.Recv(); status.Code(err) != tc.want {
			t.Errorf("%s watching %v: %v, want %v", tc.caller, tc.request, err, tc.want)
		}
	}
}

func TestIdentityWatchFollowsIdentitiesForReaderServicesOnly(t *testing.T) {
	flux := map[string]string{policyapi.FluxKustomizationNameLabel: "fake-kustomization"}
	api, kube := fixture(t, testIdentity("git-origin-identity", flux))
	stream, err := api["app"].WatchIdentities(testContext(t), &policypb.WatchIdentitiesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	got := first.GetSnapshot().GetIdentities()
	if len(got) != 1 || got[0].GetIdentity().GetName() != "git-origin-identity" || got[0].GetSpec().GetDescription() != "Fake agent git-origin-identity" ||
		got[0].GetLabels()[policyapi.FluxKustomizationNameLabel] != "fake-kustomization" {
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
	denied, err := api["unlisted"].WatchIdentities(testContext(t), &policypb.WatchIdentitiesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := denied.Recv(); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("unlisted service watching identities: %v", err)
	}
}

func TestDeleteIdentityRemovesAnUnreferencedIdentityExactly(t *testing.T) {
	identity := testIdentity("unreferenced-identity", nil)
	api, kube := fixture(t, identity, identityBinding("own-launch-binding", "unreferenced-identity", identity))
	_, err := api["app"].DeleteIdentity(testContext(t), &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "unreferenced-identity", Uid: "uid-unreferenced-identity"})
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
	api, kube := fixture(
		t, identity,
		identityCredential("fake-workloads", "other-agent", testIdentity("other-identity", nil)),
		identityCredential("fake-workloads", "previous-agent", previous),
		&workloadapi.WorkloadCredential{
			ObjectMeta: metav1.ObjectMeta{Namespace: "policies", Name: "account-agent"},
			Spec:       workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{}},
		},
	)
	if _, err := api["app"].DeleteIdentity(testContext(t), &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "unreferenced-identity", Uid: "uid-unreferenced-identity"}); err != nil {
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
	api, kube := fixture(
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
		_, err := api["app"].DeleteIdentity(testContext(t), &policypb.DeleteIdentityRequest{Namespace: "policies", Name: tc.identity.Name, Uid: string(tc.identity.UID)})
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

func TestDeleteIdentityRefusesNameOnlyCredentialUntilRemoved(t *testing.T) {
	identity := testIdentity("credentialed-identity", nil)
	credential := identityCredential("fake-workloads", "standing-agent", identity)
	credential.Spec.Subject.Identity.UID = ""
	api, kube := fixture(t, identity, credential)
	request := &policypb.DeleteIdentityRequest{Namespace: identity.Namespace, Name: identity.Name, Uid: string(identity.UID)}
	_, err := api["app"].DeleteIdentity(testContext(t), request)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("delete with name-only credential: %v, want FailedPrecondition", err)
	}
	if got, want := refusalViolations(err), []string{"WorkloadCredential/fake-workloads/standing-agent"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("refusal names %v, want %v", got, want)
	}
	var kept policyapi.Identity
	if err := kube.Get(t.Context(), client.ObjectKeyFromObject(identity), &kept); err != nil || kept.UID != identity.UID {
		t.Fatalf("refused deletion changed Identity: %+v, %v", kept, err)
	}
	if err := kube.Delete(t.Context(), credential); err != nil {
		t.Fatal(err)
	}
	if _, err := api["app"].DeleteIdentity(testContext(t), request); err != nil {
		t.Fatalf("delete after credential removal: %v", err)
	}
	if err := kube.Get(t.Context(), client.ObjectKeyFromObject(identity), &policyapi.Identity{}); !apierrors.IsNotFound(err) {
		t.Fatalf("deleted Identity remains: %v", err)
	}
}

func TestDeleteIdentityRefusesWhatItMustNotRemove(t *testing.T) {
	referenced := testIdentity("referenced-identity", nil)
	// A binding owned by another incarnation of the same name is not this identity's own.
	previous := testIdentity("referenced-identity", nil)
	previous.UID = "uid-previous-incarnation"
	api, kube := fixture(
		t,
		referenced, identityBinding("standing-grant", "referenced-identity", nil), identityBinding("stale-owner", "referenced-identity", previous),
		testIdentity("git-origin-identity", map[string]string{policyapi.FluxKustomizationNameLabel: "fake-kustomization"}),
	)
	for _, tc := range []struct {
		name    string
		caller  string
		request *policypb.DeleteIdentityRequest
		want    codes.Code
	}{
		{"referenced", "app", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "referenced-identity", Uid: "uid-referenced-identity"}, codes.FailedPrecondition},
		{"git origin", "app", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "git-origin-identity", Uid: "uid-git-origin-identity"}, codes.FailedPrecondition},
		{"replaced incarnation", "app", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "git-origin-identity", Uid: "uid-replaced"}, codes.Aborted},
		{"another namespace", "app", &policypb.DeleteIdentityRequest{Namespace: "other-policies", Name: "referenced-identity", Uid: "uid-referenced-identity"}, codes.NotFound},
		{"missing UID", "app", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "referenced-identity"}, codes.InvalidArgument},
		{"non-writer", "unlisted", &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "referenced-identity", Uid: "uid-referenced-identity"}, codes.PermissionDenied},
	} {
		_, err := api[tc.caller].DeleteIdentity(testContext(t), tc.request)
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
	api, kube := fixture(t)
	created, err := api["app"].CreateIdentity(testContext(t), &policypb.CreateIdentityRequest{Name: "launch-a", Description: "Agent of launch a"})
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
	if _, err := api["app"].CreateIdentity(testContext(t), &policypb.CreateIdentityRequest{Name: "launch-a", Description: "again"}); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("second create: %v", err)
	}
	if _, err := api["app"].CreateIdentity(testContext(t), &policypb.CreateIdentityRequest{Name: "launch-b"}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("create without description: %v", err)
	}
	if _, err := api["unlisted"].CreateIdentity(testContext(t), &policypb.CreateIdentityRequest{Name: "launch-c", Description: "x"}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("non-writer create: %v", err)
	}
}

func TestGrantIdentityPolicyBindingIsOwnedByTheExactIdentity(t *testing.T) {
	identity := testIdentity("launch-a", nil)
	api, kube := fixture(t, identity, policySet("base"))
	granted, err := api["app"].GrantIdentityPolicyBinding(testContext(t), &policypb.GrantIdentityPolicyBindingRequest{
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
	if _, err := api["app"].DeleteIdentity(testContext(t), &policypb.DeleteIdentityRequest{Namespace: "policies", Name: "launch-a", Uid: "uid-launch-a"}); err != nil {
		t.Fatalf("delete with only owned grants: %v", err)
	}
}

func TestGrantIdentityPolicyBindingRefusesAnotherIncarnationAndUnknownSets(t *testing.T) {
	api, _ := fixture(t, testIdentity("launch-a", nil), policySet("base"))
	for name, request := range map[string]*policypb.GrantIdentityPolicyBindingRequest{
		"stale UID":   {IdentityName: "launch-a", IdentityUid: "uid-previous", PolicySets: []string{"base"}},
		"missing":     {IdentityName: "launch-z", IdentityUid: "uid-launch-z", PolicySets: []string{"base"}},
		"unknown set": {IdentityName: "launch-a", IdentityUid: "uid-launch-a", PolicySets: []string{"nope"}},
		"no sets":     {IdentityName: "launch-a", IdentityUid: "uid-launch-a"},
	} {
		if _, err := api["app"].GrantIdentityPolicyBinding(testContext(t), request); err == nil {
			t.Errorf("%s: granted", name)
		}
	}
}

type bindingFrames struct {
	grpc.ServerStream
	frames []*policypb.WatchPolicyBindingsResponse
}

func (s *bindingFrames) Send(frame *policypb.WatchPolicyBindingsResponse) error {
	s.frames = append(s.frames, frame)
	return nil
}

func TestPolicyWatchRevalidatesServiceCertificateBeforeEachFrame(t *testing.T) {
	ca := transporttesting.NewCA(t)
	files := ca.Issue(t, peertls.AppURI)
	pair, err := tls.LoadX509KeyPair(files.CertFile, files.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	ctx := peer.NewContext(t.Context(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{PeerCertificates: []*x509.Certificate{leaf}}}})
	rule, err := auth.Services([]string{peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{policypb.PolicyService_WatchPolicyBindings_FullMethodName: rule}
	ctx, err = methods.AdmitContext(ctx, policypb.PolicyService_WatchPolicyBindings_FullMethodName, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	stream := &bindingFrames{}
	frame := &policypb.WatchPolicyBindingsResponse{}
	if err := send(ctx, stream, frame); err != nil {
		t.Fatal(err)
	}
	leaf.NotAfter = time.Now().Add(-time.Minute)
	if err := send(ctx, stream, frame); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("expired watch: %v", err)
	}
	if len(stream.frames) != 1 {
		t.Fatalf("sent %d frames", len(stream.frames))
	}
}
