package policycontroller

import (
	"context"
	"maps"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	policyNamespace  = "test-policies"
	subjectNamespace = "test-subjects"
	namespaceReader  = "test-namespace-reader"
	nodeReader       = "test-node-reader"
)

var grantRequest = reconcile.Request{NamespacedName: client.ObjectKey{Namespace: policyNamespace, Name: "test-grant"}}

type scope struct{ namespace, role string }

var granted = map[scope]bool{{"test-team-a", namespaceReader}: true, {"test-team-b", namespaceReader}: true, {"", nodeReader}: true}

func newReconciler(t *testing.T, expiresAt *metav1.Time, clusterRoles ...string) (*Reconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	objects := []client.Object{
		&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: subjectNamespace, Name: "test-account", UID: "uid-account"}},
		&PolicySet{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-diagnostics"},
			Spec: PolicySetSpec{Rules: []Rule{
				{Type: "kubernetes_role_binding", Role: &RoleRef{Kind: "ClusterRole", Name: namespaceReader}, Namespaces: []string{"test-team-a", "test-team-b"}},
				{Type: "kubernetes_cluster_role_binding", ClusterRole: nodeReader},
			}},
		},
		&PolicyBinding{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-grant", UID: "uid-grant"},
			Spec: PolicyBindingSpec{
				Subject:    ServiceAccountRef{Namespace: subjectNamespace, Name: "test-account"},
				PolicySets: []string{"test-diagnostics"},
				ExpiresAt:  expiresAt,
			},
		},
	}
	for _, name := range clusterRoles {
		objects = append(objects, &rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: name}})
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).WithStatusSubresource(&PolicyBinding{}).Build()
	return &Reconciler{Client: c, Roles: c, PolicyNamespace: policyNamespace, SubjectNamespaces: []string{subjectNamespace}}, c
}

func reconcileOnce(t *testing.T, r *Reconciler) reconcile.Result {
	t.Helper()
	result, err := r.Reconcile(context.Background(), grantRequest)
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func nativeScopes(t *testing.T, c client.Client) map[scope]bool {
	t.Helper()
	var roleBindings rbacv1.RoleBindingList
	var clusterBindings rbacv1.ClusterRoleBindingList
	if err := c.List(context.Background(), &roleBindings); err != nil {
		t.Fatal(err)
	}
	if err := c.List(context.Background(), &clusterBindings); err != nil {
		t.Fatal(err)
	}
	out := map[scope]bool{}
	for _, rb := range roleBindings.Items {
		out[scope{rb.Namespace, rb.RoleRef.Name}] = true
	}
	for _, crb := range clusterBindings.Items {
		out[scope{"", crb.RoleRef.Name}] = true
	}
	return out
}

func condition(t *testing.T, c client.Client) metav1.Condition {
	t.Helper()
	var binding PolicyBinding
	if err := c.Get(context.Background(), grantRequest.NamespacedName, &binding); err != nil {
		t.Fatal(err)
	}
	found := meta.FindStatusCondition(binding.Status.Conditions, KubernetesReady)
	if found == nil {
		t.Fatal("no KubernetesReady condition")
	}
	return *found
}

func TestBindingGrantsItsSetsUntilDeleted(t *testing.T) {
	r, c := newReconciler(t, nil, namespaceReader, nodeReader)
	reconcileOnce(t, r)
	if got := nativeScopes(t, c); !maps.Equal(got, granted) {
		t.Fatalf("natives = %v", got)
	}
	if got := condition(t, c); got.Status != metav1.ConditionTrue || got.Reason != "Applied" {
		t.Fatalf("condition = %+v", got)
	}
	if err := c.Delete(context.Background(), &PolicyBinding{ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-grant"}}); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if got := nativeScopes(t, c); len(got) != 0 {
		t.Fatalf("natives after delete = %v", got)
	}
}

func TestNativeBindingDeletedByHandIsRecreated(t *testing.T) {
	r, c := newReconciler(t, nil, namespaceReader, nodeReader)
	reconcileOnce(t, r)
	var clusterBindings rbacv1.ClusterRoleBindingList
	if err := c.List(context.Background(), &clusterBindings); err != nil {
		t.Fatal(err)
	}
	if err := c.Delete(context.Background(), &clusterBindings.Items[0]); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if got := nativeScopes(t, c); !maps.Equal(got, granted) {
		t.Fatalf("natives = %v", got)
	}
}

func TestExpiredBindingIsDeletedAndItsGrantsWithIt(t *testing.T) {
	expiresAt := metav1.NewTime(time.Now().Add(time.Hour))
	r, c := newReconciler(t, &expiresAt, namespaceReader, nodeReader)
	if result := reconcileOnce(t, r); result.RequeueAfter <= 0 || result.RequeueAfter > time.Hour {
		t.Fatalf("RequeueAfter = %v", result.RequeueAfter)
	}
	var binding PolicyBinding
	if err := c.Get(context.Background(), grantRequest.NamespacedName, &binding); err != nil {
		t.Fatal(err)
	}
	binding.Spec.ExpiresAt = &metav1.Time{Time: time.Now().Add(-time.Second)}
	if err := c.Update(context.Background(), &binding); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if err := c.Get(context.Background(), grantRequest.NamespacedName, &binding); !apierrors.IsNotFound(err) {
		t.Fatalf("Get after expiry: err = %v", err)
	}
	// The deletion event runs the NotFound reconcile, which removes what the binding granted.
	reconcileOnce(t, r)
	if got := nativeScopes(t, c); len(got) != 0 {
		t.Fatalf("natives after expiry = %v", got)
	}
}

func TestMissingRoleIsReportedAndLeftUnbound(t *testing.T) {
	r, c := newReconciler(t, nil, namespaceReader)
	reconcileOnce(t, r)
	if got := condition(t, c); got.Reason != "ApplyFailed" {
		t.Fatalf("condition = %+v", got)
	}
	want := map[scope]bool{{"test-team-a", namespaceReader}: true, {"test-team-b", namespaceReader}: true}
	if got := nativeScopes(t, c); !maps.Equal(got, want) {
		t.Fatalf("natives = %v", got)
	}
}

func TestMissingSetIsReportedAndTheRestStillGrant(t *testing.T) {
	r, c := newReconciler(t, nil, namespaceReader, nodeReader)
	var binding PolicyBinding
	if err := c.Get(context.Background(), grantRequest.NamespacedName, &binding); err != nil {
		t.Fatal(err)
	}
	binding.Spec.PolicySets = append([]string{"test-absent"}, binding.Spec.PolicySets...)
	if err := c.Update(context.Background(), &binding); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if got := condition(t, c); got.Reason != "PolicySetUnavailable" {
		t.Fatalf("condition = %+v", got)
	}
	if got := nativeScopes(t, c); !maps.Equal(got, granted) {
		t.Fatalf("natives = %v", got)
	}
}
