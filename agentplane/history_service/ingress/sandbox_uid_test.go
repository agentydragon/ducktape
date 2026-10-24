package ingress

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func TestServiceAccountSandboxUIDResolverTracksAccountIncarnations(t *testing.T) {
	controller := true
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: "sandbox-namespace", Name: "same-name", UID: "service-account-first",
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
			Name: "same-name", UID: "sandbox-first", Controller: &controller,
		}},
	}}
	client := fake.NewSimpleClientset(account)
	resolver := NewServiceAccountSandboxUIDResolver(client)
	writer := storage.Writer{Namespace: account.Namespace, Name: account.Name, UID: string(account.UID)}
	if got, err := resolver.Resolve(t.Context(), writer); err != nil || got != "sandbox-first" {
		t.Fatalf("first resolve = %q, %v; want sandbox-first", got, err)
	}

	account = account.DeepCopy()
	account.UID = "service-account-replacement"
	account.OwnerReferences[0].UID = "sandbox-replacement"
	if _, err := client.CoreV1().ServiceAccounts(account.Namespace).Update(t.Context(), account, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	writer.UID = string(account.UID)
	if got, err := resolver.Resolve(t.Context(), writer); err != nil || got != "sandbox-replacement" {
		t.Fatalf("replacement resolve = %q, %v; want sandbox-replacement", got, err)
	}
}

func TestServiceAccountSandboxUIDResolverRejectsRecreatedAccount(t *testing.T) {
	client := fake.NewSimpleClientset(&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: "sandbox-namespace", Name: "same-name", UID: "service-account-replacement",
	}})
	resolver := NewServiceAccountSandboxUIDResolver(client)
	_, err := resolver.Resolve(t.Context(), storage.Writer{
		Namespace: "sandbox-namespace", Name: "same-name", UID: "service-account-deleted",
	})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("resolve error = %v, want PermissionDenied", err)
	}
}
