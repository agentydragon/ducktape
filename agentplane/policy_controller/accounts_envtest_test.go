package policycontroller

import (
	"context"
	"fmt"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

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
	if status := readManagedAccount(t, api, account.Name).Status; status.ServiceAccount != nil {
		t.Fatalf("status names a ServiceAccount the controller did not create: %+v", status)
	}

	if err := api.Delete(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	waitForCachedNotFound(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(foreign))
	if _, err := r.ReconcileManagedServiceAccount(ctx, request); err != nil {
		t.Fatal(err)
	}
	created := readServiceAccount(t, api, foreign.Name)
	if created.Labels[ManagedAccountUIDLabel] != string(account.UID) || readManagedAccount(t, api, account.Name).Status.ServiceAccount.UID != created.UID {
		t.Fatalf("created ServiceAccount %+v for account uid=%s", created.ObjectMeta, account.UID)
	}

	// A new account of the same name does not take over its predecessor's ServiceAccount.
	if err := api.Delete(ctx, account); err != nil {
		t.Fatal(err)
	}
	successor := testManagedAccount(account.Name)
	create(t, api, successor)
	waitForCached(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(created))
	waitForCachedAccount(t, cached, successor)
	if _, err := r.ReconcileManagedServiceAccount(ctx, request); err != nil {
		t.Fatal(err)
	}
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
	waitForCached(t, cached, &policyapi.KubernetesAccountGrant{}, client.ObjectKeyFromObject(grant))
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
	eventually(t, "the conflicting grant's condition", func() (bool, string) {
		return grantReady(t, api, grant.Name, ConflictingExpiry), ""
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
	eventually(t, "the grant to bind once the conflict is removed", func() (bool, string) {
		natives := grantNatives(t, api, grant.Name)
		return grantReady(t, api, grant.Name, "Applied") && len(natives) == 1, describe(natives)
	})
}

func ownedBy(account *policyapi.ManagedServiceAccount) metav1.OwnerReference {
	return metav1.OwnerReference{
		APIVersion: policyapi.SchemeGroupVersion.String(),
		Kind:       "ManagedServiceAccount",
		Name:       account.Name,
		UID:        account.UID,
	}
}

func readGrant(t *testing.T, api client.Client, name string) *policyapi.KubernetesAccountGrant {
	t.Helper()
	var grant policyapi.KubernetesAccountGrant
	if err := api.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: name}, &grant); err != nil {
		t.Fatal(err)
	}
	return &grant
}

func testManagedAccount(name string) *policyapi.ManagedServiceAccount {
	return &policyapi.ManagedServiceAccount{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: name},
		Spec:       policyapi.ManagedServiceAccountSpec{APIServer: LocalAPIServer},
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
