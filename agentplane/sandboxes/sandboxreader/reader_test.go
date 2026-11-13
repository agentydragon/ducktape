package sandboxreader

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sync/atomic"
	"testing"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var (
	namespaces atomic.Int32
	apiClient  client.Client
)

// TestMain runs one API server for every test; each test works in a namespace of its own.
func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) (err error) {
		scheme := runtime.NewScheme()
		for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme} {
			if err := add(scheme); err != nil {
				return err
			}
		}
		apiClient, err = client.New(server.Config, client.Options{Scheme: scheme})
		return err
	}))
}

// fixture is one test's namespace on the shared API server.
type fixture struct {
	t         *testing.T
	namespace string
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	f := fixture{t: t, namespace: fmt.Sprintf("test-%d", namespaces.Add(1))}
	f.create(&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: f.namespace}})
	return f
}

// create stores object, filling in what the API server assigns, such as its UID.
func (f fixture) create(object client.Object) {
	f.t.Helper()
	if err := apiClient.Create(context.Background(), object); err != nil {
		f.t.Fatal(err)
	}
}

func (f fixture) reader() *Reader {
	f.t.Helper()
	reader, err := New(f.namespace, apiClient)
	if err != nil {
		f.t.Fatal(err)
	}
	return reader
}

func (f fixture) sandbox(name string) *sandboxapi.Sandbox {
	f.t.Helper()
	resource := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: f.namespace, Name: name},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template"},
	}
	f.create(resource)
	return resource
}

// executionAccount is the ServiceAccount named for Sandbox sandbox-a, with owner as its only owner reference.
func (f fixture) executionAccount(owner metav1.OwnerReference) *corev1.ServiceAccount {
	f.t.Helper()
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: f.namespace, Name: "sandbox-a", OwnerReferences: []metav1.OwnerReference{owner},
	}}
	f.create(account)
	return account
}

func (f fixture) target(name string, uid types.UID) *Identity {
	return &Identity{Namespace: f.namespace, Name: name, Uid: string(uid)}
}

func sandboxControllerReference(name string, uid types.UID, controller *bool) metav1.OwnerReference {
	return metav1.OwnerReference{
		APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
		Name: name, UID: uid, Controller: controller,
	}
}

func TestExecutionAccountRequiresTheExactLiveControllerOwnedAccount(t *testing.T) {
	controlled := true
	foreign := false
	f := newFixture(t)
	resource := f.sandbox("sandbox-a")
	account := f.executionAccount(sandboxControllerReference("sandbox-a", resource.UID, &controlled))
	identity, found, err := f.reader().ExecutionAccount(context.Background(), f.target("sandbox-a", resource.UID))
	if err != nil || !found || !proto.Equal(identity, &ServiceAccountIdentity{
		Account: &identitypb.ServiceAccount{Namespace: f.namespace, Name: "sandbox-a"}, Uid: string(account.UID),
	}) {
		t.Fatalf("ExecutionAccount() = %#v, %v, %v", identity, found, err)
	}

	cases := []struct {
		name  string
		owner func(types.UID) metav1.OwnerReference
	}{
		{"foreign Sandbox UID", func(types.UID) metav1.OwnerReference {
			return sandboxControllerReference("sandbox-a", "other-uid", &controlled)
		}},
		{"foreign Sandbox name", func(uid types.UID) metav1.OwnerReference {
			return sandboxControllerReference("sandbox-b", uid, &controlled)
		}},
		{"non-controlling reference", func(uid types.UID) metav1.OwnerReference {
			return sandboxControllerReference("sandbox-a", uid, &foreign)
		}},
		{"reference without controller bit", func(uid types.UID) metav1.OwnerReference {
			return sandboxControllerReference("sandbox-a", uid, nil)
		}},
		{"wrong API version", func(uid types.UID) metav1.OwnerReference {
			return metav1.OwnerReference{APIVersion: "other/v1", Kind: sandboxapi.SandboxKind, Name: "sandbox-a", UID: uid, Controller: &controlled}
		}},
		{"wrong kind", func(uid types.UID) metav1.OwnerReference {
			return metav1.OwnerReference{APIVersion: sandboxapi.GroupVersion.String(), Kind: "Other", Name: "sandbox-a", UID: uid, Controller: &controlled}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture(t)
			resource := f.sandbox("sandbox-a")
			f.executionAccount(tc.owner(resource.UID))
			got, found, err := f.reader().ExecutionAccount(context.Background(), f.target("sandbox-a", resource.UID))
			if err != nil || found || got != nil {
				t.Fatalf("ExecutionAccount() = %#v, %v, %v; want no usable identity", got, found, err)
			}
		})
	}
}

func TestExecutionAccountFailsClosedForMissingOrReplacedResources(t *testing.T) {
	controlled := true
	t.Run("replaced Sandbox UID", func(t *testing.T) {
		f := newFixture(t)
		resource := f.sandbox("sandbox-a")
		f.executionAccount(sandboxControllerReference("sandbox-a", resource.UID, &controlled))
		got, found, err := f.reader().ExecutionAccount(context.Background(), f.target("sandbox-a", "uid-old"))
		if err != nil || found || got != nil {
			t.Fatalf("ExecutionAccount() = %#v, %v, %v; want no usable identity", got, found, err)
		}
	})
	t.Run("missing ServiceAccount", func(t *testing.T) {
		f := newFixture(t)
		resource := f.sandbox("sandbox-a")
		got, found, err := f.reader().ExecutionAccount(context.Background(), f.target("sandbox-a", resource.UID))
		if err != nil || found || got != nil {
			t.Fatalf("ExecutionAccount() = %#v, %v, %v; want no usable identity", got, found, err)
		}
	})
	t.Run("deleting ServiceAccount", func(t *testing.T) {
		f := newFixture(t)
		resource := f.sandbox("sandbox-a")
		account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
			Namespace: f.namespace, Name: "sandbox-a", Finalizers: []string{"agentplane.allegedly.works/test-keep"},
			OwnerReferences: []metav1.OwnerReference{sandboxControllerReference("sandbox-a", resource.UID, &controlled)},
		}}
		f.create(account)
		if err := apiClient.Delete(context.Background(), account); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			account.Finalizers = nil
			_ = apiClient.Update(context.Background(), account)
		})
		if err := apiClient.Get(context.Background(), client.ObjectKeyFromObject(account), account); err != nil || account.DeletionTimestamp == nil {
			t.Fatalf("the account is not being deleted: %v", err)
		}
		got, found, err := f.reader().ExecutionAccount(context.Background(), f.target("sandbox-a", resource.UID))
		if err != nil || found || got != nil {
			t.Fatalf("ExecutionAccount() = %#v, %v, %v; want no usable identity", got, found, err)
		}
	})
}

// failingObjectReader fails reads of one kind, a fault the API server does not produce on demand.
type failingObjectReader struct {
	client.Reader
	sandboxErr, accountErr error
}

func (r failingObjectReader) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	switch object.(type) {
	case *sandboxapi.Sandbox:
		if r.sandboxErr != nil {
			return r.sandboxErr
		}
	case *corev1.ServiceAccount:
		if r.accountErr != nil {
			return r.accountErr
		}
	}
	return r.Reader.Get(ctx, key, object, options...)
}

func TestExecutionAccountPropagatesKubernetesReadErrorsWithoutIdentity(t *testing.T) {
	controlled := true
	f := newFixture(t)
	resource := f.sandbox("sandbox-a")
	f.executionAccount(sandboxControllerReference("sandbox-a", resource.UID, &controlled))
	for _, tc := range []struct {
		name string
		fail failingObjectReader
	}{
		{name: "Sandbox read", fail: failingObjectReader{sandboxErr: errors.New("api unavailable")}},
		{name: "ServiceAccount read", fail: failingObjectReader{accountErr: errors.New("api unavailable")}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tc.fail.Reader = apiClient
			reader, err := New(f.namespace, tc.fail)
			if err != nil {
				t.Fatal(err)
			}
			got, found, err := reader.ExecutionAccount(context.Background(), f.target("sandbox-a", resource.UID))
			if err == nil || found || got != nil {
				t.Fatalf("ExecutionAccount() = %#v, %v, %v; want error and no usable identity", got, found, err)
			}
		})
	}
}

func destination(sandbox string, uid types.UID, targetNamespace, ownerNamespace, ownerName string) *identitypb.SandboxDestination {
	return &identitypb.SandboxDestination{
		Target: &identitypb.EnvironmentTarget{Namespace: targetNamespace, Name: sandbox, Uid: string(uid)}, Owner: &identitypb.ServiceAccount{Namespace: ownerNamespace, Name: ownerName},
	}
}

func TestGetReadsTheSandboxAndItsIdentity(t *testing.T) {
	f := newFixture(t)
	resource := f.sandbox("sandbox-a")
	view, err := f.reader().Get(context.Background(), "sandbox-a")
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(view.Identity, f.target("sandbox-a", resource.UID)) || view.Resource.Name != "sandbox-a" {
		t.Fatalf("view = %#v", view)
	}
}

// uidlessReader reads Sandboxes without their UID, which no API server serves.
type uidlessReader struct{ client.Reader }

func (r uidlessReader) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	err := r.Reader.Get(ctx, key, object, options...)
	object.SetUID("")
	return err
}

func TestGetRefusesWhatHasNoSandboxOrNoUsableIdentity(t *testing.T) {
	f := newFixture(t)
	f.sandbox("no-uid")
	reader, err := New(f.namespace, uidlessReader{apiClient})
	if err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]codes.Code{"absent": codes.NotFound, "no-uid": codes.Unavailable} {
		if _, err := reader.Get(context.Background(), name); status.Code(err) != want {
			t.Errorf("Get(%q) = %v, want %v", name, err, want)
		}
	}
}

func TestIncarnationHoldsARequestToTheSandboxItNames(t *testing.T) {
	f := newFixture(t)
	a, b := f.sandbox("sandbox-a"), f.sandbox("sandbox-b")
	reader := f.reader()
	ns := f.namespace
	cases := []struct {
		name        string
		destination *identitypb.SandboxDestination
		want        codes.Code
	}{
		{"named incarnation", destination("sandbox-a", a.UID, ns, ns, "sandbox-a"), codes.OK},
		{"no destination", nil, codes.InvalidArgument},
		{"no UID", destination("sandbox-a", "", ns, ns, "sandbox-a"), codes.InvalidArgument},
		{"no owner", &identitypb.SandboxDestination{Target: f.target("sandbox-a", a.UID)}, codes.InvalidArgument},
		{"target namespace differs from owner", destination("sandbox-a", a.UID, "elsewhere", ns, "sandbox-a"), codes.NotFound},
		{"replaced incarnation", destination("sandbox-a", "uid-old", ns, ns, "sandbox-a"), codes.NotFound},
		{"another Sandbox's UID", destination("sandbox-a", b.UID, ns, ns, "sandbox-a"), codes.NotFound},
		{"owner is another Sandbox's account", destination("sandbox-a", a.UID, ns, ns, "sandbox-b"), codes.NotFound},
		{"owner is in another namespace", destination("sandbox-a", a.UID, "elsewhere", "elsewhere", "sandbox-a"), codes.NotFound},
		{"no such Sandbox", destination("sandbox-c", "uid-c", ns, ns, "sandbox-c"), codes.NotFound},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			view, err := reader.Incarnation(context.Background(), c.destination)
			if status.Code(err) != c.want {
				t.Fatalf("code = %v (%v), want %v", status.Code(err), err, c.want)
			}
			if c.want == codes.OK && view.Identity.Uid != string(a.UID) {
				t.Fatalf("view = %#v", view)
			}
		})
	}
}

func TestNewRequiresANamespaceAndAClient(t *testing.T) {
	if _, err := New("", apiClient); err == nil {
		t.Error("an empty namespace was accepted")
	}
	if _, err := New("test-namespace", nil); err == nil {
		t.Error("a missing client was accepted")
	}
}

func TestExecutionIdentityReadsTheNamedIdentityOfTheExactLiveSandbox(t *testing.T) {
	f := newFixture(t)
	named := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: f.namespace, Name: "names-identity"},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template", Identity: &sandboxapi.IdentityRef{Name: "present-identity"}},
	}
	dangling := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: f.namespace, Name: "names-missing-identity"},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template", Identity: &sandboxapi.IdentityRef{Name: "missing-identity"}},
	}
	identity := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: f.namespace, Name: "present-identity"},
		Spec:       policyapi.IdentitySpec{Description: "test identity"},
	}
	for _, object := range []client.Object{named, dangling, identity} {
		f.create(object)
	}
	unnamed := f.sandbox("names-no-identity")
	reader := f.reader()
	for _, tc := range []struct {
		name    string
		sandbox *Identity
		want    *identitypb.Identity
		found   bool
	}{
		{"named Identity", f.target("names-identity", named.UID), &identitypb.Identity{Name: "present-identity", Uid: string(identity.UID)}, true},
		{"names none", f.target("names-no-identity", unnamed.UID), nil, true},
		{"named Identity absent", f.target("names-missing-identity", dangling.UID), nil, false},
		{"replaced Sandbox", f.target("names-identity", "uid-replaced"), nil, false},
		{"absent Sandbox", f.target("absent", "uid-absent"), nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, found, err := reader.ExecutionIdentity(context.Background(), tc.sandbox)
			if err != nil || found != tc.found || !proto.Equal(got, tc.want) {
				t.Fatalf("ExecutionIdentity = %v, %v, %v; want %v, %v", got, found, err, tc.want, tc.found)
			}
		})
	}
}
