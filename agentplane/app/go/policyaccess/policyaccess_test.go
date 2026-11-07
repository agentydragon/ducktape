package policyaccess

import (
	"context"
	"reflect"
	"testing"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const testNamespace = "test-sandboxes"

func newAccess(t *testing.T, objects ...client.Object) (*Access, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	objectsClient := clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
	return New(testNamespace, objectsClient), objectsClient
}

func policySet(name string) *policyapi.PolicySet {
	return &policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: name}}
}

func TestPolicySetNamesListsEveryKindInNameOrder(t *testing.T) {
	access, _ := newAccess(
		t,
		policySet("z-policy"),
		policySet("a-policy"),
	)
	names, err := access.PolicySetNames(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(names, []string{"a-policy", "z-policy"}) {
		t.Fatalf("PolicySetNames() = %v", names)
	}
}
