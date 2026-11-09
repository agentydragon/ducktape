package policycontroller

import (
	"context"
	"fmt"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

const teamNamespace = "test-team-a"

// A grant on a ServiceAccount Flux created binds the role sets' roles, leaves the account itself
// alone, and its deletion takes them back.
func TestGrantOnFluxAccountBindsUntilRevoked(t *testing.T) {
	ctx := context.Background()
	api, _, _ := startPolicyAPIServer(t, true)
	setUpGrantCluster(t, api)
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: accountsNamespace, Name: "test-flux-account"}}
	set := testRoleSet("test-diagnostics", roleBindingRule(namespaceReader, teamNamespace), clusterRoleBindingRule(nodeReader))
	grant := testGrant("test-flux-grant", accountsNamespace, account.Name, set.Name)
	remote := testGrant("test-remote-grant", accountsNamespace, account.Name, set.Name)
	remote.Spec.Account.APIServer = "test-remote-apiserver"
	create(t, api, account, set, grant, remote)

	eventually(t, "the grant's native bindings", func() (bool, string) {
		natives := grantNatives(t, api, grant.Name)
		return grantReady(t, api, grant.Name, "Applied") && scopesOf(natives) == fmt.Sprintf("[%s/%s %s]", teamNamespace, namespaceReader, nodeReader) &&
			allBind(natives, account), describe(natives)
	})
	eventually(t, "the remote grant's condition", func() (bool, string) {
		return grantReady(t, api, remote.Name, "APIServerNotConfigured"), ""
	})
	if natives := grantNatives(t, api, remote.Name); len(natives) != 0 {
		t.Fatalf("a grant on a remote apiserver bound locally: %s", describe(natives))
	}
	var current corev1.ServiceAccount
	if err := api.Get(ctx, client.ObjectKeyFromObject(account), &current); err != nil {
		t.Fatal(err)
	}
	if len(current.Labels) != 0 || len(current.Annotations) != 0 {
		t.Fatalf("the controller changed a ServiceAccount it did not create: %+v", current.ObjectMeta)
	}

	if err := api.Delete(ctx, grant); err != nil {
		t.Fatal(err)
	}
	eventually(t, "the revoked grant's native bindings to be pruned", func() (bool, string) {
		natives := grantNatives(t, api, grant.Name)
		return len(natives) == 0, describe(natives)
	})
}

// Grants on one account are independent: each keeps its own native bindings, a grant whose role
// set is missing still grants the rest, and revoking one leaves the other's bindings untouched.
func TestGrantsOnOneAccountCombineAndRevokeIndependently(t *testing.T) {
	ctx := context.Background()
	api, _, _ := startPolicyAPIServer(t, true)
	setUpGrantCluster(t, api)
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: accountsNamespace, Name: "test-shared-account"}}
	reader := testRoleSet("test-namespace-read", roleBindingRule(namespaceReader, teamNamespace))
	nodes := testRoleSet("test-node-read", clusterRoleBindingRule(nodeReader))
	standing := testGrant("test-standing-grant", accountsNamespace, account.Name, reader.Name, "test-missing-set")
	widening := testGrant("test-widening-grant", accountsNamespace, account.Name, reader.Name, nodes.Name)
	create(t, api, account, reader, nodes, standing, widening)

	eventually(t, "both grants' native bindings", func() (bool, string) {
		kept, wide := grantNatives(t, api, standing.Name), grantNatives(t, api, widening.Name)
		return grantReady(t, api, standing.Name, RoleSetUnavailable) && grantReady(t, api, widening.Name, "Applied") &&
				scopesOf(kept) == fmt.Sprintf("[%s/%s]", teamNamespace, namespaceReader) &&
				scopesOf(wide) == fmt.Sprintf("[%s/%s %s]", teamNamespace, namespaceReader, nodeReader),
			describe(kept) + " / " + describe(wide)
	})
	kept := grantNatives(t, api, standing.Name)[0]

	if err := api.Delete(ctx, widening); err != nil {
		t.Fatal(err)
	}
	eventually(t, "the revoked grant's native bindings to be pruned", func() (bool, string) {
		natives := grantNatives(t, api, widening.Name)
		return len(natives) == 0, describe(natives)
	})
	natives := grantNatives(t, api, standing.Name)
	if len(natives) != 1 || natives[0].GetUID() != kept.GetUID() {
		t.Fatalf("revoking one grant changed another's native bindings: had %s, now %s", describe([]client.Object{kept}), describe(natives))
	}
}

// RBAC matches a ServiceAccount by name, so a same-name account recreated while a grant is live gets
// new native bindings, and the previous incarnation's are deleted rather than carried over.
func TestRecreatedAccountIsBoundAfreshNotAdopted(t *testing.T) {
	ctx := context.Background()
	api, _, _ := startPolicyAPIServer(t, true)
	setUpGrantCluster(t, api)
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: subjectNamespace, Name: "test-recreated-account"}}
	set := testRoleSet("test-namespace-read", roleBindingRule(namespaceReader, teamNamespace))
	grant := testGrant("test-live-grant", subjectNamespace, account.Name, set.Name)
	create(t, api, account, set, grant)
	eventually(t, "the grant's native binding", func() (bool, string) {
		natives := grantNatives(t, api, grant.Name)
		return len(natives) == 1 && natives[0].GetAnnotations()[SubjectUIDAnn] == string(account.UID), describe(natives)
	})
	old := grantNatives(t, api, grant.Name)[0]

	if err := api.Delete(ctx, account); err != nil {
		t.Fatal(err)
	}
	eventually(t, "the missing account's grants to be pruned", func() (bool, string) {
		natives := grantNatives(t, api, grant.Name)
		return grantReady(t, api, grant.Name, "SubjectMissing") && len(natives) == 0, describe(natives)
	})
	replacement := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: subjectNamespace, Name: account.Name}}
	create(t, api, replacement)
	eventually(t, "the replacement account's native binding", func() (bool, string) {
		natives := grantNatives(t, api, grant.Name)
		return len(natives) == 1 && natives[0].GetAnnotations()[SubjectUIDAnn] == string(replacement.UID) &&
			natives[0].GetName() != old.GetName(), describe(natives)
	})
	if err := api.Get(ctx, client.ObjectKeyFromObject(old), old.DeepCopyObject().(client.Object)); !apierrors.IsNotFound(err) {
		t.Fatalf("the previous account's native binding survived: err=%v", err)
	}
}

// The reconcile that expires a grant also deletes its native bindings: nothing else runs between.
func TestExpiredGrantRevokesInOnePass(t *testing.T) {
	ctx := context.Background()
	api, cached, r := startPolicyAPIServer(t, false)
	setUpGrantCluster(t, api)
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: accountsNamespace, Name: "test-flux-account"}}
	set := testRoleSet("test-node-read", clusterRoleBindingRule(nodeReader))
	grant := testGrant("test-expiring-grant", accountsNamespace, account.Name, set.Name)
	create(t, api, account, set, grant)
	waitForCached(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(account))
	waitForCached(t, cached, &policyapi.KubernetesRoleSet{}, client.ObjectKeyFromObject(set))
	waitForCached(t, cached, &policyapi.KubernetesAccountGrant{}, client.ObjectKeyFromObject(grant))
	request := reconcile.Request{NamespacedName: client.ObjectKeyFromObject(grant)}
	if _, err := r.ReconcileGrant(ctx, request); err != nil {
		t.Fatal(err)
	}
	if natives := grantNatives(t, api, grant.Name); len(natives) != 1 {
		t.Fatalf("the grant's native bindings: %s", describe(natives))
	}

	current := readGrant(t, api, grant.Name)
	current.Spec.ExpiresAt = &metav1.Time{Time: time.Now().Add(-time.Second)}
	if err := api.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	eventually(t, "the expiry in the cache", func() (bool, string) {
		var cachedGrant policyapi.KubernetesAccountGrant
		err := cached.Get(ctx, client.ObjectKeyFromObject(grant), &cachedGrant)
		return err == nil && cachedGrant.Spec.ExpiresAt != nil, fmt.Sprint(err)
	})
	if _, err := r.ReconcileGrant(ctx, request); err != nil {
		t.Fatal(err)
	}
	grantErr := api.Get(ctx, client.ObjectKeyFromObject(grant), &policyapi.KubernetesAccountGrant{})
	if natives := grantNatives(t, api, grant.Name); len(natives) != 0 || !apierrors.IsNotFound(grantErr) {
		t.Fatalf("after the expiring reconcile: grant %v, natives %s", grantErr, describe(natives))
	}
}

func setUpGrantCluster(t *testing.T, api client.Client) {
	t.Helper()
	for _, name := range []string{policyNamespace, subjectNamespace, accountsNamespace, teamNamespace} {
		create(t, api, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}})
	}
	for _, name := range []string{namespaceReader, nodeReader} {
		create(t, api, &rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: name}})
	}
}

func create(t *testing.T, api client.Client, objects ...client.Object) {
	t.Helper()
	for _, object := range objects {
		if err := api.Create(context.Background(), object); err != nil {
			t.Fatal(err)
		}
	}
}

func testRoleSet(name string, rules ...policyapi.KubernetesRule) *policyapi.KubernetesRoleSet {
	return &policyapi.KubernetesRoleSet{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: name},
		Spec:       policyapi.KubernetesRoleSetSpec{Rules: rules},
	}
}

func roleBindingRule(clusterRole string, namespaces ...string) policyapi.KubernetesRule {
	return policyapi.KubernetesRule{Type: "role_binding", Role: &policyapi.RoleRef{Kind: "ClusterRole", Name: clusterRole}, Namespaces: namespaces}
}

func clusterRoleBindingRule(clusterRole string) policyapi.KubernetesRule {
	return policyapi.KubernetesRule{Type: "cluster_role_binding", ClusterRole: clusterRole}
}

func testGrant(name, namespace, account string, roleSets ...string) *policyapi.KubernetesAccountGrant {
	return &policyapi.KubernetesAccountGrant{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: name},
		Spec: policyapi.KubernetesAccountGrantSpec{
			Account:  policyapi.KubernetesAccount{APIServer: LocalAPIServer, Namespace: namespace, Name: account},
			RoleSets: roleSets,
		},
	}
}

// eventually polls check until it holds, failing with the last state it described.
func eventually(t *testing.T, what string, check func() (bool, string)) {
	t.Helper()
	var state string
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		var ok bool
		if ok, state = check(); ok {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s; last state: %s", what, state)
}

// grantReady reports whether the grant's KubernetesReady condition has reason.
func grantReady(t *testing.T, api client.Client, name, reason string) bool {
	t.Helper()
	var grant policyapi.KubernetesAccountGrant
	if err := api.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: name}, &grant); err != nil {
		t.Fatal(err)
	}
	condition := meta.FindStatusCondition(grant.Status.Conditions, KubernetesReady)
	return condition != nil && condition.Reason == reason && condition.ObservedGeneration == grant.Generation
}

func grantNatives(t *testing.T, api client.Client, grantName string) []client.Object {
	t.Helper()
	var roleBindings rbacv1.RoleBindingList
	var clusterBindings rbacv1.ClusterRoleBindingList
	if err := api.List(context.Background(), &roleBindings); err != nil {
		t.Fatal(err)
	}
	if err := api.List(context.Background(), &clusterBindings); err != nil {
		t.Fatal(err)
	}
	var out []client.Object
	for i := range roleBindings.Items {
		if roleBindings.Items[i].Annotations[GrantAnnotation] == grantName {
			out = append(out, &roleBindings.Items[i])
		}
	}
	for i := range clusterBindings.Items {
		if clusterBindings.Items[i].Annotations[GrantAnnotation] == grantName {
			out = append(out, &clusterBindings.Items[i])
		}
	}
	return out
}

// scopesOf lists each native binding as namespace/role, or role for a ClusterRoleBinding, RoleBindings first.
func scopesOf(natives []client.Object) string {
	var out []string
	for _, native := range natives {
		ref, _ := nativeRoleAndSubjects(native)
		if native.GetNamespace() == "" {
			out = append(out, ref.Name)
		} else {
			out = append(out, native.GetNamespace()+"/"+ref.Name)
		}
	}
	return fmt.Sprint(out)
}

func allBind(natives []client.Object, account *corev1.ServiceAccount) bool {
	for _, native := range natives {
		_, subjects := nativeRoleAndSubjects(native)
		if len(subjects) != 1 || subjects[0] != (rbacv1.Subject{Kind: "ServiceAccount", Namespace: account.Namespace, Name: account.Name}) ||
			native.GetAnnotations()[SubjectUIDAnn] != string(account.UID) {
			return false
		}
	}
	return true
}

func describe(natives []client.Object) string {
	var out []string
	for _, native := range natives {
		out = append(out, fmt.Sprintf("%T %s subject-uid=%s", native, client.ObjectKeyFromObject(native), native.GetAnnotations()[SubjectUIDAnn]))
	}
	return fmt.Sprint(out)
}
