package sandboxreader

import (
	"context"
	"testing"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
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
	reader, err := New(testNamespace, clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(sandboxes...).Build())
	if err != nil {
		t.Fatal(err)
	}
	return reader
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
