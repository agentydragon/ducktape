package policyaccess

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/policy"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const testNamespace = "test-sandboxes"

func policyScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{
		policyapi.AddToScheme, egressapi.AddToScheme, sandboxapi.AddToScheme,
	} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	return scheme
}

func newAccess(t *testing.T, objects ...client.Object) (*Access, client.Client) {
	t.Helper()
	objectsClient := clientfake.NewClientBuilder().WithScheme(policyScheme(t)).WithObjects(objects...).Build()
	return New(testNamespace, objectsClient), objectsClient
}

func policySet(name string, generation int64, rules ...policyapi.Rule) *policyapi.PolicySet {
	return &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: name, Generation: generation},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	}
}

func binding(name string, subject policyapi.ServiceAccountRef, sets ...string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: name, Generation: 3},
		Spec:       policyapi.PolicyBindingSpec{Subject: subject, PolicySets: sets},
	}
}

func TestPolicySetNamesListsEveryKindInNameOrder(t *testing.T) {
	access, _ := newAccess(
		t,
		policySet("z-policy", 1, policyapi.Rule{Type: "egress", Hosts: []string{"example.test"}}),
		policySet("a-policy", 1, policyapi.Rule{Type: "action_auto_approve"}),
	)
	names, err := access.PolicySetNames(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(names, []string{"a-policy", "z-policy"}) {
		t.Fatalf("PolicySetNames() = %v", names)
	}
}

type generatedNameClient struct {
	client.Client
	created int
}

func (c *generatedNameClient) List(ctx context.Context, list client.ObjectList, options ...client.ListOption) error {
	if _, ok := list.(*policyapi.PolicySetList); !ok {
		return fmt.Errorf("unexpected list after grant: %T", list)
	}
	return c.Client.List(ctx, list, options...)
}

func (c *generatedNameClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	if object.GetGenerateName() != "" {
		c.created++
		object.SetName(fmt.Sprintf("%s%05d", object.GetGenerateName(), c.created))
		object.SetGeneration(1)
	}
	return c.Client.Create(ctx, object, options...)
}

func TestGrantValidatesPolicySetsAndCreatesIndependentSandboxOwnedBindings(t *testing.T) {
	base := clientfake.NewClientBuilder().WithScheme(policyScheme(t)).WithObjects(
		policySet("known", 5, policyapi.Rule{Type: "action_auto_approve"}),
	).Build()
	objects := &generatedNameClient{Client: base}
	access := New(testNamespace, objects)
	sandbox := &environmentpb.EnvironmentDescriptor{
		Target:           &identitypb.EnvironmentTarget{Namespace: testNamespace, Name: "worker", Uid: "sandbox-uid"},
		ExecutionAccount: &identitypb.ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: testNamespace, Name: "worker"}},
	}
	expiresAt := &metav1.Time{Time: time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)}
	if err := access.Grant(context.Background(), sandbox, []string{"known"}, expiresAt); err != nil {
		t.Fatal(err)
	}
	if err := access.Grant(context.Background(), sandbox, []string{"known"}, nil); err != nil {
		t.Fatal(err)
	}
	stored := &policyapi.PolicyBinding{}
	if err := base.Get(context.Background(), client.ObjectKey{Namespace: testNamespace, Name: "worker-00001"}, stored); err != nil {
		t.Fatal(err)
	}
	if stored.GenerateName != "worker-" || stored.Spec.Subject != (policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "worker"}) ||
		!reflect.DeepEqual(stored.Spec.PolicySets, []string{"known"}) || stored.Spec.ExpiresAt == nil ||
		!stored.Spec.ExpiresAt.Time.Equal(expiresAt.Time) || len(stored.OwnerReferences) != 1 {
		t.Fatalf("stored PolicyBinding = %#v", stored)
	}
	owner := stored.OwnerReferences[0]
	if owner.APIVersion != sandboxapi.GroupVersion.String() || owner.Kind != sandboxapi.SandboxKind ||
		owner.Name != sandbox.GetTarget().GetName() || owner.UID != types.UID(sandbox.GetTarget().GetUid()) || owner.Controller == nil || *owner.Controller ||
		owner.BlockOwnerDeletion == nil || *owner.BlockOwnerDeletion {
		t.Fatalf("Sandbox owner reference = %#v", owner)
	}
	second := &policyapi.PolicyBinding{}
	if err := base.Get(context.Background(), client.ObjectKey{Namespace: testNamespace, Name: "worker-00002"}, second); err != nil {
		t.Fatalf("second independent binding missing: %v", err)
	}
	err := access.Grant(context.Background(), sandbox, []string{"z-missing", "a-missing", "z-missing"}, nil)
	var unknown *policy.UnknownPolicySetError
	if !errors.As(err, &unknown) || !reflect.DeepEqual(unknown.Names, []string{"a-missing", "z-missing"}) {
		t.Fatalf("Grant with missing sets = %v", err)
	}
	if objects.created != 2 {
		t.Fatalf("unknown PolicySets created %d bindings", objects.created-2)
	}
}

func TestRevokeDeletesOnlyOperatorOwnedBindings(t *testing.T) {
	operator := binding("operator", policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "worker"}, "known")
	git := binding("git", policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "worker"}, "known")
	git.Labels = map[string]string{fluxKustomizationLabel: "agentplane-staging"}
	sandboxOwned := binding("sandbox", policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "worker"}, "known")
	controller := true
	sandboxOwned.OwnerReferences = []metav1.OwnerReference{{Kind: sandboxapi.SandboxKind, Controller: &controller}}
	access, objects := newAccess(t, operator, git, sandboxOwned)
	if err := access.Revoke(context.Background(), "operator"); err != nil {
		t.Fatal(err)
	}
	if err := objects.Get(context.Background(), client.ObjectKey{Namespace: testNamespace, Name: "operator"}, &policyapi.PolicyBinding{}); !apierrors.IsNotFound(err) {
		t.Fatalf("operator binding was not deleted: %v", err)
	}
	var fromGit *FluxOwnedBindingError
	if err := access.Revoke(context.Background(), "git"); !errors.As(err, &fromGit) {
		t.Fatalf("revoke Git-owned binding = %v", err)
	}
	var fromSandbox *SandboxOwnedBindingError
	if err := access.Revoke(context.Background(), "sandbox"); !errors.As(err, &fromSandbox) {
		t.Fatalf("revoke Sandbox-owned binding = %v", err)
	}
	for _, name := range []string{"git", "sandbox"} {
		if err := objects.Get(context.Background(), client.ObjectKey{Namespace: testNamespace, Name: name}, &policyapi.PolicyBinding{}); err != nil {
			t.Fatalf("refused binding %q was changed: %v", name, err)
		}
	}
	var missing *BindingNotFoundError
	if err := access.Revoke(context.Background(), "absent"); !errors.As(err, &missing) {
		t.Fatalf("revoke missing binding = %v", err)
	}
}
