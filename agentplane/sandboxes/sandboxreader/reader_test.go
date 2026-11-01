package sandboxreader

import (
	"context"
	"errors"
	"testing"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const testNamespace = "testing"

func newReader(t *testing.T, sandboxes ...client.Object) *Reader {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	reader, err := New(testNamespace, clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(sandboxes...).Build())
	if err != nil {
		t.Fatal(err)
	}
	return reader
}

func executionAccount(uid types.UID, owner metav1.OwnerReference) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: testNamespace, Name: "sandbox-a", UID: uid,
			OwnerReferences: []metav1.OwnerReference{owner},
		},
	}
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
	valid := sandboxControllerReference("sandbox-a", "uid-a", &controlled)
	reader := newReader(
		t,
		sandbox(testNamespace, "sandbox-a", "uid-a"),
		executionAccount("account-uid", valid),
	)
	identity, found, err := reader.ExecutionAccount(context.Background(), Identity{Namespace: testNamespace, Name: "sandbox-a", UID: "uid-a"})
	if err != nil || !found || identity != (ServiceAccountIdentity{Namespace: testNamespace, Name: "sandbox-a", UID: "account-uid"}) {
		t.Fatalf("ExecutionAccount() = %#v, %v, %v", identity, found, err)
	}

	cases := []struct {
		name  string
		owner metav1.OwnerReference
	}{
		{name: "foreign Sandbox UID", owner: sandboxControllerReference("sandbox-a", "other-uid", &controlled)},
		{name: "foreign Sandbox name", owner: sandboxControllerReference("sandbox-b", "uid-a", &controlled)},
		{name: "non-controlling reference", owner: sandboxControllerReference("sandbox-a", "uid-a", &foreign)},
		{name: "reference without controller bit", owner: sandboxControllerReference("sandbox-a", "uid-a", nil)},
		{name: "wrong API version", owner: metav1.OwnerReference{APIVersion: "other/v1", Kind: sandboxapi.SandboxKind, Name: "sandbox-a", UID: "uid-a", Controller: &controlled}},
		{name: "wrong kind", owner: metav1.OwnerReference{APIVersion: sandboxapi.GroupVersion.String(), Kind: "Other", Name: "sandbox-a", UID: "uid-a", Controller: &controlled}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reader := newReader(t, sandbox(testNamespace, "sandbox-a", "uid-a"), executionAccount("account-uid", tc.owner))
			got, found, err := reader.ExecutionAccount(context.Background(), Identity{Namespace: testNamespace, Name: "sandbox-a", UID: "uid-a"})
			if err != nil || found || got != (ServiceAccountIdentity{}) {
				t.Fatalf("ExecutionAccount() = %#v, %v, %v; want no usable identity", got, found, err)
			}
		})
	}
}

func TestExecutionAccountFailsClosedForMissingOrReplacedResources(t *testing.T) {
	controlled := true
	owner := sandboxControllerReference("sandbox-a", "uid-current", &controlled)
	cases := []struct {
		name    string
		objects []client.Object
		sandbox Identity
	}{
		{
			name: "replaced Sandbox UID",
			objects: []client.Object{
				sandbox(testNamespace, "sandbox-a", "uid-current"), executionAccount("account-uid", owner),
			},
			sandbox: Identity{Namespace: testNamespace, Name: "sandbox-a", UID: "uid-old"},
		},
		{
			name:    "missing ServiceAccount",
			objects: []client.Object{sandbox(testNamespace, "sandbox-a", "uid-current")},
			sandbox: Identity{Namespace: testNamespace, Name: "sandbox-a", UID: "uid-current"},
		},
		{
			name: "deleting ServiceAccount",
			objects: []client.Object{
				sandbox(testNamespace, "sandbox-a", "uid-current"),
				deletingAccount("account-uid", owner),
			},
			sandbox: Identity{Namespace: testNamespace, Name: "sandbox-a", UID: "uid-current"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reader := newReader(t, tc.objects...)
			got, found, err := reader.ExecutionAccount(context.Background(), tc.sandbox)
			if err != nil || found || got != (ServiceAccountIdentity{}) {
				t.Fatalf("ExecutionAccount() = %#v, %v, %v; want no usable identity", got, found, err)
			}
		})
	}
}

func deletingAccount(uid types.UID, owner metav1.OwnerReference) *corev1.ServiceAccount {
	now := metav1.Now()
	account := executionAccount(uid, owner)
	account.DeletionTimestamp = &now
	account.Finalizers = []string{"keep-until-test"}
	return account
}

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
	owner := sandboxControllerReference("sandbox-a", "uid-a", &controlled)
	objects := clientfake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(
		sandbox(testNamespace, "sandbox-a", "uid-a"), executionAccount("account-uid", owner),
	).Build()
	for _, tc := range []struct {
		name string
		fail failingObjectReader
	}{
		{name: "Sandbox read", fail: failingObjectReader{sandboxErr: errors.New("api unavailable")}},
		{name: "ServiceAccount read", fail: failingObjectReader{accountErr: errors.New("api unavailable")}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tc.fail.Reader = objects
			reader, err := New(testNamespace, tc.fail)
			if err != nil {
				t.Fatal(err)
			}
			got, found, err := reader.ExecutionAccount(context.Background(), Identity{Namespace: testNamespace, Name: "sandbox-a", UID: "uid-a"})
			if err == nil || found || got != (ServiceAccountIdentity{}) {
				t.Fatalf("ExecutionAccount() = %#v, %v, %v; want error and no usable identity", got, found, err)
			}
		})
	}
}

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return scheme
}

func sandbox(namespace, name string, uid types.UID) *sandboxapi.Sandbox {
	return &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name, UID: uid}}
}

func destination(sandbox, uid, ownerNamespace, ownerName string) *sandboxespb.SandboxDestination {
	return &sandboxespb.SandboxDestination{
		Sandbox: sandbox, SandboxUid: uid,
		Owner: &sandboxespb.ServiceAccount{Namespace: ownerNamespace, Name: ownerName},
	}
}

func TestGetReadsTheSandboxAndItsIdentity(t *testing.T) {
	reader := newReader(t, sandbox(testNamespace, "sandbox-a", "uid-a"))
	view, err := reader.Get(context.Background(), "sandbox-a")
	if err != nil {
		t.Fatal(err)
	}
	if view.Identity != (Identity{Namespace: testNamespace, Name: "sandbox-a", UID: "uid-a"}) || view.Resource.Name != "sandbox-a" {
		t.Fatalf("view = %#v", view)
	}
}

func TestGetRefusesWhatHasNoSandboxOrNoUsableIdentity(t *testing.T) {
	reader := newReader(t, sandbox(testNamespace, "no-uid", ""))
	for name, want := range map[string]codes.Code{"absent": codes.NotFound, "no-uid": codes.Unavailable} {
		if _, err := reader.Get(context.Background(), name); status.Code(err) != want {
			t.Errorf("Get(%q) = %v, want %v", name, err, want)
		}
	}
}

func TestIncarnationHoldsARequestToTheSandboxItNames(t *testing.T) {
	reader := newReader(t, sandbox(testNamespace, "sandbox-a", "uid-a"), sandbox(testNamespace, "sandbox-b", "uid-b"))
	cases := []struct {
		name        string
		destination *sandboxespb.SandboxDestination
		want        codes.Code
	}{
		{"named incarnation", destination("sandbox-a", "uid-a", testNamespace, "sandbox-a"), codes.OK},
		{"no destination", nil, codes.InvalidArgument},
		{"no UID", destination("sandbox-a", "", testNamespace, "sandbox-a"), codes.InvalidArgument},
		{"no owner", &sandboxespb.SandboxDestination{Sandbox: "sandbox-a", SandboxUid: "uid-a"}, codes.InvalidArgument},
		{"replaced incarnation", destination("sandbox-a", "uid-old", testNamespace, "sandbox-a"), codes.NotFound},
		{"another Sandbox's UID", destination("sandbox-a", "uid-b", testNamespace, "sandbox-a"), codes.NotFound},
		{"owner is another Sandbox's account", destination("sandbox-a", "uid-a", testNamespace, "sandbox-b"), codes.NotFound},
		{"owner is in another namespace", destination("sandbox-a", "uid-a", "elsewhere", "sandbox-a"), codes.NotFound},
		{"no such Sandbox", destination("sandbox-c", "uid-c", testNamespace, "sandbox-c"), codes.NotFound},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			view, err := reader.Incarnation(context.Background(), c.destination)
			if status.Code(err) != c.want {
				t.Fatalf("code = %v (%v), want %v", status.Code(err), err, c.want)
			}
			if c.want == codes.OK && view.Identity.UID != "uid-a" {
				t.Fatalf("view = %#v", view)
			}
		})
	}
}

func TestNewRequiresANamespaceAndAClient(t *testing.T) {
	if _, err := New("", clientfake.NewClientBuilder().Build()); err == nil {
		t.Error("an empty namespace was accepted")
	}
	if _, err := New(testNamespace, nil); err == nil {
		t.Error("a missing client was accepted")
	}
}
