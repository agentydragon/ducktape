package policycontroller

import (
	"context"
	"errors"
	"maps"
	"slices"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
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
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	objects := []client.Object{
		&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: subjectNamespace, Name: "test-account", UID: "uid-account"}},
		&policyapi.PolicySet{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-diagnostics"},
			Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{
				{Type: "kubernetes_role_binding", Role: &policyapi.RoleRef{Kind: "ClusterRole", Name: namespaceReader}, Namespaces: []string{"test-team-a", "test-team-b"}},
				{Type: "kubernetes_cluster_role_binding", ClusterRole: nodeReader},
			}},
		},
		&policyapi.PolicyBinding{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-grant", UID: "uid-grant"},
			Spec: policyapi.PolicyBindingSpec{
				Subject:    policyapi.ServiceAccountRef{Namespace: subjectNamespace, Name: "test-account"},
				PolicySets: []string{"test-diagnostics"},
				ExpiresAt:  expiresAt,
			},
		},
	}
	for _, name := range clusterRoles {
		objects = append(objects, &rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: name}})
	}
	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(objects...).
		WithStatusSubresource(&policyapi.PolicyBinding{}).
		WithIndex(&rbacv1.RoleBinding{}, nativeBindingIndex, bindingIndex).
		WithIndex(&rbacv1.ClusterRoleBinding{}, nativeBindingIndex, bindingIndex).
		Build()
	return &Reconciler{Client: c, APIReader: c, PolicyNamespace: policyNamespace, SubjectNamespaces: []string{subjectNamespace}}, c
}

func bindingIndex(obj client.Object) []string {
	if bindingName := obj.GetAnnotations()[BindingAnnotation]; bindingName != "" {
		return []string{bindingName}
	}
	return nil
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
	var binding policyapi.PolicyBinding
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
	if err := c.Delete(context.Background(), &policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-grant"}}); err != nil {
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

func TestNativeBindingSubjectUIDChangeReplacesGrant(t *testing.T) {
	r, c := newReconciler(t, nil, namespaceReader, nodeReader)
	reconcileOnce(t, r)
	var before rbacv1.RoleBindingList
	if err := c.List(context.Background(), &before); err != nil {
		t.Fatal(err)
	}
	oldNames := map[string]bool{}
	for _, binding := range before.Items {
		if binding.Annotations[BindingAnnotation] == grantRequest.Name {
			oldNames[binding.Namespace+"/"+binding.Name] = true
		}
	}
	if len(oldNames) != 2 {
		t.Fatalf("old subject RoleBindings = %v", oldNames)
	}

	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: subjectNamespace, Name: "test-account"}}
	if err := c.Delete(context.Background(), account); err != nil {
		t.Fatal(err)
	}
	account.UID = "uid-account-recreated"
	if err := c.Create(context.Background(), account); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)

	var after rbacv1.RoleBindingList
	if err := c.List(context.Background(), &after); err != nil {
		t.Fatal(err)
	}
	newNames := map[string]bool{}
	for _, binding := range after.Items {
		if binding.Annotations[BindingAnnotation] != grantRequest.Name {
			continue
		}
		name := binding.Namespace + "/" + binding.Name
		if oldNames[name] {
			t.Fatalf("old identity grant %s survived ServiceAccount replacement", name)
		}
		if got := binding.Annotations[SubjectUIDAnn]; got != "uid-account-recreated" {
			t.Fatalf("subject UID annotation = %q", got)
		}
		newNames[name] = true
	}
	if len(newNames) != 2 {
		t.Fatalf("new subject RoleBindings = %v", newNames)
	}
}

type missingOnceReader struct {
	client.Reader
	key    client.ObjectKey
	missed bool
}

func (r *missingOnceReader) Get(ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
	if !r.missed && key == r.key {
		r.missed = true
		return apierrors.NewNotFound(schema.GroupResource{Group: rbacv1.GroupName, Resource: "rolebindings"}, key.Name)
	}
	return r.Reader.Get(ctx, key, obj, opts...)
}

type trackingApplyClient struct {
	client.Client
	applyCalls int
}

func (c *trackingApplyClient) Apply(ctx context.Context, configuration runtime.ApplyConfiguration, opts ...client.ApplyOption) error {
	c.applyCalls++
	return c.Client.Apply(ctx, configuration, opts...)
}

func TestNativeCreateRaceDoesNotAdoptForeignBinding(t *testing.T) {
	r, c := newReconciler(t, nil, namespaceReader, nodeReader)
	reconcileOnce(t, r)
	var bindings rbacv1.RoleBindingList
	if err := c.List(context.Background(), &bindings); err != nil {
		t.Fatal(err)
	}
	wanted := bindings.Items[0].DeepCopy()
	wanted.Name = "racing-foreign-binding"
	wanted.UID = ""
	wanted.ResourceVersion = ""
	wanted.ManagedFields = nil
	wanted.CreationTimestamp = metav1.Time{}
	foreign := wanted.DeepCopy()
	foreign.Labels = map[string]string{"owner": "foreign"}
	foreign.Annotations = map[string]string{"owner": "foreign"}
	foreign.Subjects[0].Name = "foreign-account"
	if err := c.Create(context.Background(), foreign); err != nil {
		t.Fatal(err)
	}
	tracker := &trackingApplyClient{Client: c}
	r.Client = tracker
	r.APIReader = &missingOnceReader{Reader: c, key: client.ObjectKeyFromObject(wanted)}

	if err := r.applyNative(context.Background(), wanted); !apierrors.IsAlreadyExists(err) {
		t.Fatalf("applyNative error = %v, want AlreadyExists after the name claim race", err)
	}
	if tracker.applyCalls != 0 {
		t.Fatalf("apply was called %d times after exclusive create lost the race", tracker.applyCalls)
	}
	var after rbacv1.RoleBinding
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(foreign), &after); err != nil {
		t.Fatal(err)
	}
	if after.Labels["owner"] != "foreign" || after.Subjects[0].Name != "foreign-account" {
		t.Fatalf("foreign binding changed during create race: %+v", after)
	}
}

func TestRevokedNativeBindingIsDeleted(t *testing.T) {
	r, c := newReconciler(t, nil, namespaceReader, nodeReader)
	reconcileOnce(t, r)
	var set policyapi.PolicySet
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: "test-diagnostics"}, &set); err != nil {
		t.Fatal(err)
	}
	set.Spec.Rules[0].Namespaces = []string{"test-team-a"}
	set.Spec.Rules = set.Spec.Rules[:1]
	if err := c.Update(context.Background(), &set); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	want := map[scope]bool{{"test-team-a", namespaceReader}: true}
	if got := nativeScopes(t, c); !maps.Equal(got, want) {
		t.Fatalf("native grants after revocation = %v", got)
	}
}

type failingApplyClient struct {
	client.Client
	err error
}

func (c failingApplyClient) Apply(context.Context, runtime.ApplyConfiguration, ...client.ApplyOption) error {
	return c.err
}

func TestNativeApplyFailureIsReportedAndRetried(t *testing.T) {
	r, c := newReconciler(t, nil, namespaceReader, nodeReader)
	failure := errors.New("API server apply failed")
	r.Client = failingApplyClient{Client: c, err: failure}
	if _, err := r.Reconcile(context.Background(), grantRequest); !errors.Is(err, failure) {
		t.Fatalf("Reconcile error = %v, want the original apply error", err)
	}
	if got := condition(t, c); got.Status != metav1.ConditionFalse || got.Reason != "ApplyFailed" {
		t.Fatalf("failure condition = %+v", got)
	}

	r.Client = c
	reconcileOnce(t, r)
	if got := condition(t, c); got.Status != metav1.ConditionTrue || got.Reason != "Applied" {
		t.Fatalf("retry condition = %+v", got)
	}
}

func TestExpiredBindingIsDeletedAndItsGrantsWithIt(t *testing.T) {
	expiresAt := metav1.NewTime(time.Now().Add(time.Hour))
	r, c := newReconciler(t, &expiresAt, namespaceReader, nodeReader)
	if result := reconcileOnce(t, r); result.RequeueAfter <= 0 || result.RequeueAfter > time.Hour {
		t.Fatalf("RequeueAfter = %v", result.RequeueAfter)
	}
	var binding policyapi.PolicyBinding
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
	if got := condition(t, c); got.Status != metav1.ConditionFalse {
		t.Fatalf("condition = %+v", got)
	}
	var binding policyapi.PolicyBinding
	if err := c.Get(context.Background(), grantRequest.NamespacedName, &binding); err != nil {
		t.Fatal(err)
	}
	if want := []policyapi.MissingRole{{Kind: "ClusterRole", Name: nodeReader}}; !slices.Equal(binding.Status.MissingRoles, want) {
		t.Fatalf("missingRoles = %+v, want %+v", binding.Status.MissingRoles, want)
	}
	want := map[scope]bool{{"test-team-a", namespaceReader}: true, {"test-team-b", namespaceReader}: true}
	if got := nativeScopes(t, c); !maps.Equal(got, want) {
		t.Fatalf("natives = %v", got)
	}

	if err := c.Create(context.Background(), &rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: nodeReader}}); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if err := c.Get(context.Background(), grantRequest.NamespacedName, &binding); err != nil {
		t.Fatal(err)
	}
	if got := condition(t, c); got.Status != metav1.ConditionTrue || len(binding.Status.MissingRoles) != 0 {
		t.Fatalf("after the role appeared: condition = %+v, missingRoles = %+v", got, binding.Status.MissingRoles)
	}
	if got := nativeScopes(t, c); !maps.Equal(got, granted) {
		t.Fatalf("natives after the role appeared = %v", got)
	}
}

func TestMissingSetIsReportedAndTheRestStillGrant(t *testing.T) {
	r, c := newReconciler(t, nil, namespaceReader, nodeReader)
	var binding policyapi.PolicyBinding
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
