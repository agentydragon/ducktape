package policyaccess

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	appsandboxes "github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
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

func account(namespace, name string) *sandboxespb.ServiceAccount {
	return &sandboxespb.ServiceAccount{Namespace: namespace, Name: name}
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

func TestActionPolicySetsSortAndExposeOnlyActionRuleRefusals(t *testing.T) {
	access, _ := newAccess(
		t,
		policySet("z-other-kind", 4, policyapi.Rule{Type: "egress", Hosts: []string{"example.test"}}),
		policySet("b-refused", 7, policyapi.Rule{Type: "action_auto_approve"}),
		policySet("a-valid", 2, policyapi.Rule{Type: "action_auto_approve", Policy: &policyapi.ActionPolicy{
			Type: "exact_actions", Actions: map[string]policyapi.ActionNames{"test_group": {"record"}},
		}}),
	)
	views, err := access.ActionPolicySets(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(views) != 3 || views[0].Name != "a-valid" || views[0].Generation != 2 || views[0].Refused != nil ||
		views[1].Name != "b-refused" || views[1].Generation != 7 || views[1].Refused == nil ||
		!strings.HasPrefix(*views[1].Refused, "rules[0]: an action_auto_approve rule needs a policy") ||
		views[2].Name != "z-other-kind" || views[2].Refused != nil {
		t.Fatalf("ActionPolicySets() = %#v", views)
	}
}

func TestBindingsForProjectsRulesCredentialsConditionsAndProvenance(t *testing.T) {
	secretCredential := &egressapi.EgressCredential{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "github-token"},
		Spec: egressapi.EgressCredentialSpec{
			Description: "test bot token",
			Source:      egressapi.CredentialSource{SecretRef: &egressapi.SecretKeyRef{Name: "github-secret", Key: "token"}},
			Targets:     []egressapi.Target{{Method: egressapi.SchemeToken, Header: "Authorization", Scheme: "Bearer"}},
		},
	}
	workloadCredential := &egressapi.EgressCredential{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "caller-token"},
		Spec: egressapi.EgressCredentialSpec{
			Description: "the authenticated workload",
			Source:      egressapi.CredentialSource{AuthenticatedWorkloadToken: &egressapi.AuthenticatedWorkloadTokenSource{}},
			Targets:     []egressapi.Target{{Method: egressapi.WholeValue, Header: "X-Caller"}},
		},
	}
	mixed := policySet(
		"mixed", 7,
		policyapi.Rule{
			Type: "action_auto_approve",
			Policy: &policyapi.ActionPolicy{
				Type: "exact_actions", Actions: map[string]policyapi.ActionNames{"test_review": {"record"}},
			},
		},
		policyapi.Rule{
			Type: "egress", Hosts: []string{"api.github.com"}, Methods: []string{"GET", "POST"},
			CredentialRef: &policyapi.CredentialRef{Name: "github-token"},
		},
		policyapi.Rule{
			Type: "egress", Hosts: []string{"api.example.test"},
			CredentialRef: &policyapi.CredentialRef{Name: "missing-token"},
		},
		policyapi.Rule{
			Type: "egress", Hosts: []string{"identity.example.test"},
			CredentialRef: &policyapi.CredentialRef{Name: "caller-token"},
		},
	)
	broken := policySet("broken", 2, policyapi.Rule{Type: "egress", Hosts: []string{}})
	git := binding("a-git", policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "live"}, "mixed")
	git.Labels = map[string]string{fluxKustomizationLabel: "agentplane-staging"}
	sandboxOwned := binding("b-sandbox", policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "live"}, "broken")
	controller := true
	sandboxOwned.OwnerReferences = []metav1.OwnerReference{{Kind: sandboxapi.SandboxKind, Controller: &controller}}
	operator := binding("c-operator", policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "live"}, "mixed", "vanished")
	operator.Spec.ExpiresAt = &metav1.Time{Time: time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)}
	operator.Status.Conditions = []metav1.Condition{
		{Type: "EgressReady", Status: metav1.ConditionFalse, Reason: "Test", Message: "egress pending", ObservedGeneration: 3},
		{Type: "ActionsReady", Status: metav1.ConditionTrue, Reason: "Ready", Message: "actions ready", ObservedGeneration: 3},
	}
	otherNamespace := binding("d-other-namespace", policyapi.ServiceAccountRef{Namespace: "elsewhere", Name: "live"}, "mixed")
	access, _ := newAccess(t, secretCredential, workloadCredential, mixed, broken, git, sandboxOwned, operator, otherNamespace)

	views, err := access.BindingsFor(context.Background(), account(testNamespace, "live"))
	if err != nil {
		t.Fatal(err)
	}
	if len(views) != 3 || views[0].Name != "a-git" || views[1].Name != "b-sandbox" || views[2].Name != "c-operator" {
		t.Fatalf("BindingsFor names = %#v", views)
	}
	if views[0].Provenance != BindingFromGit || views[1].Provenance != BindingFromSandbox ||
		views[2].Provenance != BindingFromOperator {
		t.Fatalf("binding provenance = %q, %q, %q", views[0].Provenance, views[1].Provenance, views[2].Provenance)
	}
	operatorView := views[2]
	if operatorView.Generation != 3 || operatorView.ExpiresAt == nil ||
		!operatorView.ExpiresAt.Time.Equal(operator.Spec.ExpiresAt.Time) {
		t.Fatalf("binding metadata projection = %#v", operatorView)
	}
	if len(operatorView.Conditions) != 2 || operatorView.Conditions[0].Type != "ActionsReady" ||
		operatorView.Conditions[1].Type != "EgressReady" || operatorView.Conditions[1].ObservedGeneration == nil ||
		*operatorView.Conditions[1].ObservedGeneration != 3 {
		t.Fatalf("condition projection = %#v", operatorView.Conditions)
	}
	if !reflect.DeepEqual(operatorView.MissingPolicySets, []string{"vanished"}) || len(operatorView.PolicySets) != 1 {
		t.Fatalf("PolicySet projection = %#v / missing %v", operatorView.PolicySets, operatorView.MissingPolicySets)
	}
	set := operatorView.PolicySets[0]
	if set.Name != "mixed" || set.Generation != 7 || set.Refused != nil || len(set.Rules) != 4 {
		t.Fatalf("mixed PolicySet projection = %#v", set)
	}
	other := set.Rules[0]
	if other.Kind != "other" || other.Number != 0 || other.Type != "action_auto_approve" || other.Fields["policy"] == nil {
		t.Fatalf("other rule projection = %#v", set.Rules[0])
	}
	firstEgress := set.Rules[1]
	if firstEgress.Kind != "egress" || firstEgress.Number != 1 || !reflect.DeepEqual(firstEgress.Hosts, []string{"api.github.com"}) ||
		!reflect.DeepEqual(firstEgress.Methods, []string{"GET", "POST"}) || firstEgress.Paths != nil {
		t.Fatalf("first egress projection = %#v", set.Rules[1])
	}
	if firstEgress.Credential == nil || firstEgress.Credential.Placeholder != "agentplane-credential-github-token" ||
		firstEgress.Credential.Secret == nil || *firstEgress.Credential.Secret != "github-secret" ||
		firstEgress.Credential.Key == nil || *firstEgress.Credential.Key != "token" ||
		firstEgress.Credential.Targets[0].Method != egressapi.SchemeToken {
		t.Fatalf("safe credential projection = %#v", firstEgress.Credential)
	}
	missing := set.Rules[2]
	if missing.Kind != "egress" || missing.Credential != nil || missing.MissingCredential == nil || *missing.MissingCredential != "missing-token" {
		t.Fatalf("missing credential projection = %#v", set.Rules[2])
	}
	workload := set.Rules[3]
	if workload.Kind != "egress" || workload.Credential == nil || workload.Credential.Secret != nil || workload.Credential.Key != nil {
		t.Fatalf("workload token projection = %#v", set.Rules[3])
	}
	encoded, err := json.Marshal(set.Rules)
	if err != nil {
		t.Fatal(err)
	}
	var serialized []map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &serialized); err != nil {
		t.Fatal(err)
	}
	if len(serialized) != 4 || serialized[0]["type"] == nil || serialized[0]["fields"] == nil || serialized[0]["hosts"] != nil ||
		serialized[1]["hosts"] == nil || serialized[1]["credential"] == nil || serialized[1]["fields"] != nil {
		t.Fatalf("serialized rule variants = %s", encoded)
	}
	refused := views[1].PolicySets[0]
	if refused.Name != "broken" || len(refused.Rules) != 0 || refused.Refused == nil || *refused.Refused == "" {
		t.Fatalf("invalid egress rule was not refused as a whole: %#v", refused)
	}
}

func TestBindingsFromSnapshotMatchesDirectProjection(t *testing.T) {
	set := policySet("egress", 2, policyapi.Rule{Type: "egress", Hosts: []string{"api.example.test"}})
	item := binding("operator-grant", policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "worker"}, "egress", "missing")
	access, _ := newAccess(t, set, item)
	direct, err := access.BindingsFor(context.Background(), account(testNamespace, "worker"))
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := BindingsFromSnapshot([]policyapi.PolicyBinding{*item}, []policyapi.PolicySet{*set}, nil,
		account(testNamespace, "worker"))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(snapshot, direct) {
		t.Fatalf("snapshot projection = %#v, direct = %#v", snapshot, direct)
	}
}

func TestBindingsForRejectsInvalidCredentialProjection(t *testing.T) {
	invalid := &egressapi.EgressCredential{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "invalid"},
		Spec: egressapi.EgressCredentialSpec{
			Description: "test credential",
			Source:      egressapi.CredentialSource{AuthenticatedWorkloadToken: &egressapi.AuthenticatedWorkloadTokenSource{}},
			Targets:     []egressapi.Target{{Method: egressapi.JSONField, Header: "Authorization"}},
		},
	}
	set := policySet("uses-invalid", 1, policyapi.Rule{
		Type: "egress", Hosts: []string{"api.example.test"},
		CredentialRef: &policyapi.CredentialRef{Name: "invalid"},
	})
	access, _ := newAccess(t, invalid, set, binding("grant", policyapi.ServiceAccountRef{
		Namespace: testNamespace, Name: "worker",
	}, "uses-invalid"))

	_, err := access.BindingsFor(context.Background(), account(testNamespace, "worker"))
	if err == nil || !strings.Contains(err.Error(), `EgressCredential "invalid"`) {
		t.Fatalf("BindingsFor invalid credential = %v", err)
	}
}

type generatedNameClient struct {
	client.Client
	created int
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
	sandbox := &sandboxespb.Sandbox{
		Name: "worker", Uid: "sandbox-uid", ServiceAccount: account(testNamespace, "worker"),
	}
	expiresAt := &metav1.Time{Time: time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)}
	first, err := access.Grant(context.Background(), sandbox, []string{"known"}, expiresAt)
	if err != nil {
		t.Fatal(err)
	}
	second, err := access.Grant(context.Background(), sandbox, []string{"known"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if first.Name != "worker-00001" || second.Name != "worker-00002" {
		t.Fatalf("generated grant names = %q, %q", first.Name, second.Name)
	}
	stored := &policyapi.PolicyBinding{}
	if err := base.Get(context.Background(), client.ObjectKey{Namespace: testNamespace, Name: first.Name}, stored); err != nil {
		t.Fatal(err)
	}
	if stored.GenerateName != "worker-" || stored.Spec.Subject != (policyapi.ServiceAccountRef{Namespace: testNamespace, Name: "worker"}) ||
		!reflect.DeepEqual(stored.Spec.PolicySets, []string{"known"}) || stored.Spec.ExpiresAt == nil ||
		!stored.Spec.ExpiresAt.Time.Equal(expiresAt.Time) || len(stored.OwnerReferences) != 1 {
		t.Fatalf("stored PolicyBinding = %#v", stored)
	}
	owner := stored.OwnerReferences[0]
	if owner.APIVersion != sandboxapi.GroupVersion.String() || owner.Kind != sandboxapi.SandboxKind ||
		owner.Name != sandbox.Name || owner.UID != types.UID(sandbox.Uid) || owner.Controller == nil || *owner.Controller ||
		owner.BlockOwnerDeletion == nil || *owner.BlockOwnerDeletion {
		t.Fatalf("Sandbox owner reference = %#v", owner)
	}
	if first.Provenance != BindingFromOperator || first.PolicySets[0].Name != "known" {
		t.Fatalf("returned grant view = %#v", first)
	}
	views, err := access.BindingsFor(context.Background(), sandbox.ServiceAccount)
	if err != nil || len(views) != 2 {
		t.Fatalf("repeated grant changed existing binding rather than adding one: %#v, %v", views, err)
	}
	_, err = access.Grant(context.Background(), sandbox, []string{"z-missing", "a-missing", "z-missing"}, nil)
	var unknown *appsandboxes.UnknownPolicySetError
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
