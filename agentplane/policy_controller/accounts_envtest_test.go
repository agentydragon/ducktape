package policycontroller

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	"github.com/bazelbuild/rules_go/go/runfiles"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// A temporary account, run by the controller's watches with no restart: it creates the
// ServiceAccount and records it, a grant it owns binds, a hand-deleted ServiceAccount is recreated,
// and the account's expiresAt alone, with none on the grant, removes the account, its ServiceAccount
// and the grant's native bindings. The test apiserver runs no garbage collector, so the owned grant
// object itself outlives the account here; deleting it is the collector's job.
func TestManagedServiceAccountLifecycleThroughExpiry(t *testing.T) {
	ctx := context.Background()
	api, _, _ := startPolicyAPIServer(t, true)
	setUpGrantCluster(t, api)
	account := testManagedAccount("test-temporary-account")
	set := testRoleSet("test-node-read", clusterRoleBindingRule(nodeReader))
	create(t, api, account, set)
	grant := testGrant("test-temporary-grant", accountsNamespace, account.Name, set.Name)
	grant.OwnerReferences = []metav1.OwnerReference{ownedBy(account)}
	create(t, api, grant)

	first := waitForManagedServiceAccount(t, api, account, "")
	eventually(t, "the grant's native binding on the created account", func() (bool, string) {
		natives := grantNatives(t, api, grant.Name)
		return len(natives) == 1 && allBind(natives, first), describe(natives)
	})
	assertAccountUse(t, api, account, first)
	// A hand-deleted use set and a hand-edited credential are restored.
	if err := api.Delete(ctx, readUseSet(t, api, account.Name)); err != nil {
		t.Fatal(err)
	}
	edited := readCredential(t, api, account.Name)
	edited.Spec.Source.KubernetesServiceAccount.Name = "test-other-account"
	if err := api.Update(ctx, edited); err != nil {
		t.Fatal(err)
	}
	eventually(t, "the account's use restored", func() (bool, string) {
		var set policyapi.PolicySet
		err := api.Get(ctx, client.ObjectKey{Namespace: policyNamespace, Name: AccountCredentialName(account.Name)}, &set)
		source := readCredential(t, api, account.Name).Spec.Source.KubernetesServiceAccount
		return err == nil && source.Name == first.Name, fmt.Sprintf("PolicySet: %v, source: %+v", err, source)
	})
	assertAccountUse(t, api, account, first)

	if err := api.Delete(ctx, first); err != nil {
		t.Fatal(err)
	}
	second := waitForManagedServiceAccount(t, api, account, first.UID)
	eventually(t, "the grant's native binding on the recreated account", func() (bool, string) {
		natives := grantNatives(t, api, grant.Name)
		return len(natives) == 1 && allBind(natives, second), describe(natives)
	})

	// Moving expiresAt into the near future drives the clock: only the reconcile scheduled for the
	// deadline can act on it, since no further event arrives.
	if err := api.Get(ctx, client.ObjectKeyFromObject(account), account); err != nil {
		t.Fatal(err)
	}
	account.Spec.ExpiresAt = &metav1.Time{Time: time.Now().Add(2 * time.Second)}
	if err := api.Update(ctx, account); err != nil {
		t.Fatal(err)
	}
	eventually(t, "the expired account, its ServiceAccount and the grant's bindings to go", func() (bool, string) {
		accountErr := api.Get(ctx, client.ObjectKeyFromObject(account), &policyapi.ManagedServiceAccount{})
		serviceAccountErr := api.Get(ctx, client.ObjectKeyFromObject(second), &corev1.ServiceAccount{})
		natives := grantNatives(t, api, grant.Name)
		return apierrors.IsNotFound(accountErr) && apierrors.IsNotFound(serviceAccountErr) && len(natives) == 0 &&
				grantReady(t, api, grant.Name, "SubjectMissing"),
			fmt.Sprintf("account: %v, ServiceAccount: %v, natives: %s", accountErr, serviceAccountErr, describe(natives))
	})
}

// A ServiceAccount at a managed account's name is never adopted by name: one the controller did not
// create stays untouched until it is gone, and one created for an earlier account of the same name
// is replaced.
func TestManagedServiceAccountIsNeverAdoptedByName(t *testing.T) {
	ctx := context.Background()
	api, cached, r := startPolicyAPIServer(t, false)
	for _, name := range []string{policyNamespace, accountsNamespace} {
		create(t, api, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}})
	}
	foreign := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: accountsNamespace, Name: "test-collision"}}
	account := testManagedAccount(foreign.Name)
	create(t, api, foreign, account)
	waitForCached(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(foreign))
	waitForCachedAccount(t, cached, account)
	request := reconcile.Request{NamespacedName: client.ObjectKeyFromObject(account)}
	if _, err := r.ReconcileManagedServiceAccount(ctx, request); err == nil {
		t.Fatal("reconcile adopted a ServiceAccount it did not create")
	}
	var untouched corev1.ServiceAccount
	if err := api.Get(ctx, client.ObjectKeyFromObject(foreign), &untouched); err != nil {
		t.Fatal(err)
	}
	if untouched.UID != foreign.UID || len(untouched.Labels) != 0 || len(untouched.Annotations) != 0 {
		t.Fatalf("the foreign ServiceAccount was changed: %+v", untouched.ObjectMeta)
	}
	if status := readManagedAccount(t, api, account.Name).Status; status.ServiceAccount != nil || accountReason(status) != "ServiceAccountConflict" {
		t.Fatalf("status after a collision: %+v", status)
	}

	if err := api.Delete(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	waitForCachedNotFound(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(foreign))
	if _, err := r.ReconcileManagedServiceAccount(ctx, request); err != nil {
		t.Fatal(err)
	}
	created := readServiceAccount(t, api, foreign.Name)
	if status := readManagedAccount(t, api, account.Name).Status; created.Labels[ManagedAccountUIDLabel] != string(account.UID) ||
		status.ServiceAccount.UID != created.UID || accountReason(status) != "Created" {
		t.Fatalf("created ServiceAccount %+v for account uid=%s", created.ObjectMeta, account.UID)
	}
	assertAccountUse(t, api, account, created)

	// A new account of the same name does not take over its predecessor's ServiceAccount, nor its
	// EgressCredential and PolicySet until the garbage collector, which envtest lacks, removes them.
	if err := api.Delete(ctx, account); err != nil {
		t.Fatal(err)
	}
	successor := testManagedAccount(account.Name)
	create(t, api, successor)
	waitForCached(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(created))
	waitForCachedAccount(t, cached, successor)
	if _, err := r.ReconcileManagedServiceAccount(ctx, request); err == nil {
		t.Fatal("reconcile adopted its predecessor's EgressCredential")
	}
	if reason := accountReason(readManagedAccount(t, api, successor.Name).Status); reason != EgressCredentialConflict {
		t.Fatalf("successor's reason before its predecessor's use is collected: %s", reason)
	}
	if credential := readCredential(t, api, account.Name); !metav1.IsControlledBy(credential, account) {
		t.Fatalf("predecessor's EgressCredential taken over: %+v", credential.OwnerReferences)
	}
	collect(t, api, readCredential(t, api, account.Name), readUseSet(t, api, account.Name))
	if _, err := r.ReconcileManagedServiceAccount(ctx, request); err != nil {
		t.Fatal(err)
	}
	assertAccountUse(t, api, successor, readServiceAccount(t, api, foreign.Name))
	replaced := readServiceAccount(t, api, foreign.Name)
	if replaced.UID == created.UID || replaced.Labels[ManagedAccountUIDLabel] != string(successor.UID) ||
		readManagedAccount(t, api, successor.Name).Status.ServiceAccount.UID != replaced.UID {
		t.Fatalf("successor account uid=%s kept ServiceAccount %+v (predecessor's uid=%s)", successor.UID, replaced.ObjectMeta, created.UID)
	}

	// Deleted while the controller is not looking: the reconcile of the name prunes by UID label.
	if err := api.Delete(ctx, successor); err != nil {
		t.Fatal(err)
	}
	waitForCachedNotFound(t, cached, &policyapi.ManagedServiceAccount{}, client.ObjectKeyFromObject(successor))
	waitForCached(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(replaced))
	if _, err := r.ReconcileManagedServiceAccount(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := api.Get(ctx, client.ObjectKeyFromObject(replaced), &corev1.ServiceAccount{}); !apierrors.IsNotFound(err) {
		t.Fatalf("the gone account's ServiceAccount survived: err=%v", err)
	}
}

// The reconcile that expires an account also deletes every native binding on its ServiceAccount:
// with no grant reconcile in between, none survives that one pass.
func TestExpiredManagedAccountRevokesInOnePass(t *testing.T) {
	ctx := context.Background()
	api, cached, r := startPolicyAPIServer(t, false)
	setUpGrantCluster(t, api)
	account := testManagedAccount("test-expiring-account")
	set := testRoleSet("test-diagnostics", roleBindingRule(namespaceReader, teamNamespace), clusterRoleBindingRule(nodeReader))
	create(t, api, account, set)
	grant := testGrant("test-owned-grant", accountsNamespace, account.Name, set.Name)
	grant.OwnerReferences = []metav1.OwnerReference{ownedBy(account)}
	create(t, api, grant)
	waitForCachedAccount(t, cached, account)
	request := reconcile.Request{NamespacedName: client.ObjectKeyFromObject(account)}
	if _, err := r.ReconcileManagedServiceAccount(ctx, request); err != nil {
		t.Fatal(err)
	}
	serviceAccount := readServiceAccount(t, api, account.Name)
	waitForCached(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(serviceAccount))
	waitForCached(t, cached, &policyapi.KubernetesRoleSet{}, client.ObjectKeyFromObject(set))
	waitForCached(t, cached, &policyapi.ServiceAccountGrant{}, client.ObjectKeyFromObject(grant))
	if _, err := r.ReconcileGrant(ctx, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(grant)}); err != nil {
		t.Fatal(err)
	}
	if natives := grantNatives(t, api, grant.Name); len(natives) != 2 || !allBind(natives, serviceAccount) {
		t.Fatalf("the grant's native bindings: %s", describe(natives))
	}

	current := readManagedAccount(t, api, account.Name)
	current.Spec.ExpiresAt = &metav1.Time{Time: time.Now().Add(-time.Second)}
	if err := api.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	eventually(t, "the expiry in the cache", func() (bool, string) {
		var cachedAccount policyapi.ManagedServiceAccount
		err := cached.Get(ctx, client.ObjectKeyFromObject(account), &cachedAccount)
		return err == nil && cachedAccount.Spec.ExpiresAt != nil, fmt.Sprint(err)
	})
	if _, err := r.ReconcileManagedServiceAccount(ctx, request); err != nil {
		t.Fatal(err)
	}
	accountErr := api.Get(ctx, client.ObjectKeyFromObject(account), &policyapi.ManagedServiceAccount{})
	serviceAccountErr := api.Get(ctx, client.ObjectKeyFromObject(serviceAccount), &corev1.ServiceAccount{})
	if natives := grantNatives(t, api, grant.Name); len(natives) != 0 || !apierrors.IsNotFound(accountErr) || !apierrors.IsNotFound(serviceAccountErr) {
		t.Fatalf("after the expiring reconcile: account %v, ServiceAccount %v, natives %s", accountErr, serviceAccountErr, describe(natives))
	}
}

// A grant that a ManagedServiceAccount owns and that sets its own expiresAt grants nothing and is
// not expired, until the conflict is removed.
func TestOwnedGrantWithItsOwnExpiryConflicts(t *testing.T) {
	ctx := context.Background()
	api, _, _ := startPolicyAPIServer(t, true)
	setUpGrantCluster(t, api)
	account := testManagedAccount("test-conflict-account")
	set := testRoleSet("test-node-read", clusterRoleBindingRule(nodeReader))
	create(t, api, account, set)
	grant := testGrant("test-conflicting-grant", accountsNamespace, account.Name, set.Name)
	grant.OwnerReferences = []metav1.OwnerReference{ownedBy(account)}
	grant.Spec.ExpiresAt = &metav1.Time{Time: time.Now().Add(-time.Minute)}
	create(t, api, grant)
	eventually(t, "the conflict on the grant and its owning account", func() (bool, string) {
		status := readManagedAccount(t, api, account.Name).Status
		return grantReady(t, api, grant.Name, ConflictingExpiry) && accountReason(status) == ConflictingExpiry, fmt.Sprintf("%+v", status.Conditions)
	})
	// Still there despite its past expiresAt, and binding nothing, though its account exists.
	waitForManagedServiceAccount(t, api, account, "")
	if natives := grantNatives(t, api, grant.Name); len(natives) != 0 {
		t.Fatalf("a grant with conflicting expiry bound: %s", describe(natives))
	}

	current := readGrant(t, api, grant.Name)
	current.Spec.ExpiresAt = nil
	if err := api.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	eventually(t, "the grant to bind and the account to clear once the conflict is removed", func() (bool, string) {
		natives := grantNatives(t, api, grant.Name)
		return grantReady(t, api, grant.Name, "Applied") && len(natives) == 1 &&
			accountReason(readManagedAccount(t, api, account.Name).Status) == "Created", describe(natives)
	})
}

// An EgressCredential or PolicySet at a managed account's use name that the account does not
// control is never adopted: the account reports the conflict, the object stays untouched, and the
// use is created once it is gone.
func TestManagedAccountUseIsNeverAdopted(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		reason  string
		foreign client.Object
	}{
		{EgressCredentialConflict, &egressapi.EgressCredential{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: AccountCredentialName("test-collision")},
			Spec: egressapi.EgressCredentialSpec{
				Description: "test foreign credential",
				Source:      egressapi.CredentialSource{SecretRef: &egressapi.SecretKeyRef{Name: "test-secret", Key: "test-key"}},
				Targets:     []egressapi.Target{{Header: "X-Test", Method: egressapi.WholeValue}},
			},
		}},
		{PolicySetConflict, &policyapi.PolicySet{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: AccountCredentialName("test-collision")},
			Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: "egress", Hosts: []string{"test.example"}}}},
		}},
	} {
		t.Run(tc.reason, func(t *testing.T) {
			api, cached, r := startPolicyAPIServer(t, false)
			for _, name := range []string{policyNamespace, accountsNamespace} {
				create(t, api, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}})
			}
			account := testManagedAccount("test-collision")
			create(t, api, account, tc.foreign)
			before := tc.foreign.GetResourceVersion()
			waitForCachedAccount(t, cached, account)
			request := reconcile.Request{NamespacedName: client.ObjectKeyFromObject(account)}
			if _, err := r.ReconcileManagedServiceAccount(ctx, request); err == nil {
				t.Fatal("reconcile adopted a use object it did not create")
			}
			if err := api.Get(ctx, client.ObjectKeyFromObject(tc.foreign), tc.foreign); err != nil {
				t.Fatal(err)
			}
			if tc.foreign.GetResourceVersion() != before || len(tc.foreign.GetOwnerReferences()) != 0 {
				t.Fatalf("the foreign object was changed: %+v", tc.foreign)
			}
			if reason := accountReason(readManagedAccount(t, api, account.Name).Status); reason != tc.reason {
				t.Fatalf("reason after a collision: %s", reason)
			}

			if err := api.Delete(ctx, tc.foreign); err != nil {
				t.Fatal(err)
			}
			if _, err := r.ReconcileManagedServiceAccount(ctx, request); err != nil {
				t.Fatal(err)
			}
			if reason := accountReason(readManagedAccount(t, api, account.Name).Status); reason != "Created" {
				t.Fatalf("reason once the collision is gone: %s", reason)
			}
			assertAccountUse(t, api, account, readServiceAccount(t, api, account.Name))
		})
	}
}

// The controller's real writes of an account's use pass the admission policy cdk8s renders over them
// (cluster/cdk8s/agentplane/managed_account_admission.py), and one outside that shape does not, so
// the controller and the policy cannot drift apart on the host, prefix or shape.
func TestManagedAccountUseIsAdmitted(t *testing.T) {
	ctx := context.Background()
	controllerUser := "system:serviceaccount:" + policyNamespace + ":test-policy-controller"
	api, cached, r := startPolicyAPIServerAs(t, false, controllerUser)
	for _, name := range []string{policyNamespace, accountsNamespace} {
		create(t, api, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}})
	}
	installManagedAccountAdmission(t, api)
	account := testManagedAccount("test-admitted-account")
	create(t, api, account)
	waitForCachedAccount(t, cached, account)

	mismatched := &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: policyNamespace, Name: AccountCredentialName(account.Name),
			OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(account, policyapi.SchemeGroupVersion.WithKind("ManagedServiceAccount"))},
		},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
			Type: resources.EgressRuleType, Hosts: []string{"test.example"}, ClusterInternal: true,
			CredentialRef: &policyapi.CredentialRef{Name: AccountCredentialName(account.Name)},
		}}},
	}
	// The API server loads a new policy in the background, and shows no sign of having loaded it.
	eventually(t, "the policy to refuse the controller a use set to another host", func() (bool, string) {
		err := r.Create(ctx, mismatched.DeepCopy(), client.DryRunAll)
		return apierrors.IsInvalid(err), fmt.Sprint(err)
	})

	if _, err := r.ReconcileManagedServiceAccount(ctx, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(account)}); err != nil {
		t.Fatal(err)
	}
	assertAccountUse(t, api, account, readServiceAccount(t, api, account.Name))
	if reason := accountReason(readManagedAccount(t, api, account.Name).Status); reason != "Created" {
		t.Fatalf("reason after an admitted reconcile: %s", reason)
	}
}

// installManagedAccountAdmission creates the rendered policy and binding, named for this package's
// namespaces and the controller test-policy-controller.
func installManagedAccountAdmission(t *testing.T, api client.Client) {
	t.Helper()
	path, err := runfiles.Rlocation(os.Getenv("MANAGED_ACCOUNT_ADMISSION"))
	if err != nil {
		t.Fatal(err)
	}
	rendered, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var documents []map[string]any
	if err := json.Unmarshal(rendered, &documents); err != nil {
		t.Fatal(err)
	}
	if len(documents) != 2 {
		t.Fatalf("want the policy and its binding, rendered %d objects", len(documents))
	}
	for _, document := range documents {
		create(t, api, &unstructured.Unstructured{Object: document})
	}
}

// assertAccountUse checks the account's EgressCredential and use PolicySet: the shape cdk8s renders
// for a static account, acting as serviceAccount, and controlled by account so garbage collection
// removes them with it.
func assertAccountUse(t *testing.T, api client.Client, account *policyapi.ManagedServiceAccount, serviceAccount *corev1.ServiceAccount) {
	t.Helper()
	credential := readCredential(t, api, account.Name)
	set := readUseSet(t, api, account.Name)
	for _, owned := range []client.Object{credential, set} {
		if refs := owned.GetOwnerReferences(); len(refs) != 1 || !metav1.IsControlledBy(owned, account) ||
			refs[0].Kind != "ManagedServiceAccount" || (refs[0].BlockOwnerDeletion != nil && *refs[0].BlockOwnerDeletion) {
			t.Fatalf("%T %s ownerReferences: %+v", owned, owned.GetName(), refs)
		}
	}
	name := AccountCredentialName(account.Name)
	source := credential.Spec.Source
	if source.SecretRef != nil || source.ProjectedWorkloadToken != nil || source.KubernetesServiceAccount == nil ||
		*source.KubernetesServiceAccount != (egressapi.KubernetesServiceAccountSource{Namespace: serviceAccount.Namespace, Name: serviceAccount.Name}) {
		t.Fatalf("credential source: %+v", source)
	}
	if want := []egressapi.Target{{Header: "Authorization", Method: egressapi.SchemeToken, Scheme: "Bearer"}}; !slices.Equal(credential.Spec.Targets, want) {
		t.Fatalf("credential targets: %+v", credential.Spec.Targets)
	}
	if !strings.Contains(credential.Spec.Description, "--token "+resources.Placeholder(name)) {
		t.Fatalf("credential description names no placeholder: %s", credential.Spec.Description)
	}
	if rules := set.Spec.Rules; len(rules) != 1 || rules[0].Type != resources.EgressRuleType ||
		!slices.Equal(rules[0].Hosts, []string{localAPIServerHost}) || !rules[0].ClusterInternal ||
		rules[0].CredentialRef == nil || rules[0].CredentialRef.Name != name || len(rules[0].Methods) != 0 || len(rules[0].Paths) != 0 {
		t.Fatalf("use set rules: %+v", rules)
	}
}

// collect deletes what the garbage collector would once objs' owner is gone.
func collect(t *testing.T, api client.Client, objs ...client.Object) {
	t.Helper()
	for _, obj := range objs {
		if err := api.Delete(context.Background(), obj); err != nil {
			t.Fatal(err)
		}
	}
}

func readCredential(t *testing.T, api client.Client, account string) *egressapi.EgressCredential {
	t.Helper()
	var credential egressapi.EgressCredential
	if err := api.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: AccountCredentialName(account)}, &credential); err != nil {
		t.Fatal(err)
	}
	return &credential
}

func readUseSet(t *testing.T, api client.Client, account string) *policyapi.PolicySet {
	t.Helper()
	var set policyapi.PolicySet
	if err := api.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: AccountCredentialName(account)}, &set); err != nil {
		t.Fatal(err)
	}
	return &set
}

func ownedBy(account *policyapi.ManagedServiceAccount) metav1.OwnerReference {
	return metav1.OwnerReference{
		APIVersion: policyapi.SchemeGroupVersion.String(),
		Kind:       "ManagedServiceAccount",
		Name:       account.Name,
		UID:        account.UID,
	}
}

// accountReason is the reason of the account's KubernetesReady condition, or "" without one.
func accountReason(status policyapi.ManagedServiceAccountStatus) string {
	if condition := meta.FindStatusCondition(status.Conditions, KubernetesReady); condition != nil {
		return condition.Reason
	}
	return ""
}

func readGrant(t *testing.T, api client.Client, name string) *policyapi.ServiceAccountGrant {
	t.Helper()
	var grant policyapi.ServiceAccountGrant
	if err := api.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: name}, &grant); err != nil {
		t.Fatal(err)
	}
	return &grant
}

func testManagedAccount(name string) *policyapi.ManagedServiceAccount {
	return &policyapi.ManagedServiceAccount{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: name},
		Spec:       policyapi.ManagedServiceAccountSpec{},
	}
}

// waitForManagedServiceAccount waits for the account's labelled ServiceAccount, other than one with
// UID previous, and for the account's status to name it.
func waitForManagedServiceAccount(t *testing.T, api client.Client, account *policyapi.ManagedServiceAccount, previous types.UID) *corev1.ServiceAccount {
	t.Helper()
	var found corev1.ServiceAccount
	eventually(t, "the managed account's ServiceAccount", func() (bool, string) {
		if err := api.Get(context.Background(), client.ObjectKey{Namespace: accountsNamespace, Name: account.Name}, &found); err != nil {
			return false, err.Error()
		}
		status := readManagedAccount(t, api, account.Name).Status.ServiceAccount
		want := policyapi.CreatedServiceAccount{Namespace: accountsNamespace, Name: account.Name, UID: found.UID}
		return found.UID != previous && found.Labels[ManagedAccountUIDLabel] == string(account.UID) && status != nil && *status == want,
			fmt.Sprintf("ServiceAccount %+v, status %+v", found.ObjectMeta, status)
	})
	return &found
}

func waitForCachedAccount(t *testing.T, cached client.Client, account *policyapi.ManagedServiceAccount) {
	t.Helper()
	eventually(t, "the managed account in the cache", func() (bool, string) {
		var current policyapi.ManagedServiceAccount
		err := cached.Get(context.Background(), client.ObjectKeyFromObject(account), &current)
		return err == nil && current.UID == account.UID, fmt.Sprintf("uid=%s err=%v", current.UID, err)
	})
}

func readManagedAccount(t *testing.T, api client.Client, name string) *policyapi.ManagedServiceAccount {
	t.Helper()
	var account policyapi.ManagedServiceAccount
	if err := api.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: name}, &account); err != nil {
		t.Fatal(err)
	}
	return &account
}

func readServiceAccount(t *testing.T, api client.Client, name string) *corev1.ServiceAccount {
	t.Helper()
	var account corev1.ServiceAccount
	if err := api.Get(context.Background(), client.ObjectKey{Namespace: accountsNamespace, Name: name}, &account); err != nil {
		t.Fatal(err)
	}
	return &account
}
