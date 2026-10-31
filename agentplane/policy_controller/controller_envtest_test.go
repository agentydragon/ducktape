package policycontroller

import (
	"context"
	"fmt"
	"slices"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
)

func TestAPIServerApplyDriftRevocationIdentityAndForeignCollision(t *testing.T) {
	ctx := context.Background()
	api, cached, r := startPolicyAPIServer(t, false)
	for _, name := range []string{policyNamespace, subjectNamespace, "test-team-a", "test-team-b"} {
		if err := api.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{namespaceReader, nodeReader, "replacement-reader"} {
		if err := api.Create(ctx, &rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: name}}); err != nil {
			t.Fatal(err)
		}
	}
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: subjectNamespace, Name: "test-account"}}
	if err := api.Create(ctx, account); err != nil {
		t.Fatal(err)
	}
	set := testPolicySet("test-diagnostics", []string{"test-team-a", "test-team-b"}, true)
	if err := api.Create(ctx, set); err != nil {
		t.Fatal(err)
	}
	binding := testPolicyBinding("test-grant", account.Name, set.Name)
	if err := api.Create(ctx, binding); err != nil {
		t.Fatal(err)
	}
	if err := api.Get(ctx, client.ObjectKeyFromObject(binding), binding); err != nil {
		t.Fatal(err)
	}
	binding.Status.Conditions = []metav1.Condition{{
		Type:               "EgressReady",
		Status:             metav1.ConditionTrue,
		LastTransitionTime: metav1.Now(),
		Reason:             "Applied",
		Message:            "egress reader status",
	}}
	if err := api.Status().Update(ctx, binding); err != nil {
		t.Fatal(err)
	}
	waitForCached(t, cached, &policyapi.PolicyBinding{}, client.ObjectKeyFromObject(binding))
	waitForCachedCondition(t, cached, binding.Name, "EgressReady")
	waitForCached(t, cached, &policyapi.PolicySet{}, client.ObjectKeyFromObject(set))
	waitForCached(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(account))
	if _, err := r.Reconcile(ctx, reconcileRequest(binding.Name)); err != nil {
		t.Fatal(err)
	}
	roleBindings, clusterBindings := nativeBindingsFor(t, api, binding.Name)
	if len(roleBindings) != 2 || len(clusterBindings) != 1 {
		t.Fatalf("initial native bindings: RoleBindings=%d ClusterRoleBindings=%d", len(roleBindings), len(clusterBindings))
	}
	for _, native := range nativeObjects(roleBindings, clusterBindings) {
		managed := managedFields(native)
		if !hasApplyManager(managed) {
			t.Fatalf("%T %s has no server-side apply manager entry: %+v", native, client.ObjectKeyFromObject(native), managed)
		}
	}
	current := readPolicyBinding(t, api, binding.Name)
	if condition := conditionFor(current, KubernetesReady); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("KubernetesReady condition = %+v", condition)
	}
	if condition := conditionFor(current, "EgressReady"); condition == nil || condition.Message != "egress reader status" {
		t.Fatalf("another reader's condition was lost: %+v", condition)
	}

	for _, native := range nativeObjects(roleBindings, clusterBindings) {
		drifted := native.DeepCopyObject().(client.Object)
		switch obj := drifted.(type) {
		case *rbacv1.RoleBinding:
			obj.Subjects[0].Name = "someone-else"
		case *rbacv1.ClusterRoleBinding:
			obj.Subjects[0].Name = "someone-else"
		}
		if err := api.Update(ctx, drifted); err != nil {
			t.Fatal(err)
		}
		waitForCachedSubjectName(t, cached, drifted, "someone-else")
	}
	if _, err := r.Reconcile(ctx, reconcileRequest(binding.Name)); err != nil {
		t.Fatal(err)
	}
	roleBindings, clusterBindings = nativeBindingsFor(t, api, binding.Name)
	for _, native := range nativeObjects(roleBindings, clusterBindings) {
		if !slices.ContainsFunc(nativeSubjects(native), func(subject rbacv1.Subject) bool {
			return subject.Kind == "ServiceAccount" && subject.Namespace == subjectNamespace && subject.Name == account.Name
		}) {
			t.Fatalf("SSA did not repair subjects on %T %s: %+v", native, client.ObjectKeyFromObject(native), nativeSubjects(native))
		}
	}

	roleBinding := roleBindings[0]
	if roleBinding.Namespace != "test-team-a" {
		for _, candidate := range roleBindings {
			if candidate.Namespace == "test-team-a" {
				roleBinding = candidate
			}
		}
	}
	if err := api.Delete(ctx, roleBinding); err != nil {
		t.Fatal(err)
	}
	waitForCachedNotFound(t, cached, &rbacv1.RoleBinding{}, client.ObjectKeyFromObject(roleBinding))
	replacement := roleBinding.DeepCopy()
	replacement.ResourceVersion = ""
	replacement.UID = ""
	replacement.CreationTimestamp = metav1.Time{}
	replacement.ManagedFields = nil
	replacement.RoleRef.Name = "replacement-reader"
	if err := api.Create(ctx, replacement); err != nil {
		t.Fatal(err)
	}
	waitForCachedRoleRef(t, cached, replacement.Namespace, replacement.Name, "replacement-reader")
	if _, err := r.Reconcile(ctx, reconcileRequest(binding.Name)); err == nil {
		t.Fatal("reconcile did not request a retry after deleting the immutable roleRef mismatch")
	}
	waitForCachedNotFound(t, cached, &rbacv1.RoleBinding{}, client.ObjectKeyFromObject(replacement))
	if _, err := r.Reconcile(ctx, reconcileRequest(binding.Name)); err != nil {
		t.Fatal(err)
	}
	var repaired rbacv1.RoleBinding
	if err := api.Get(ctx, client.ObjectKeyFromObject(replacement), &repaired); err != nil {
		t.Fatal(err)
	}
	if repaired.RoleRef.Name != namespaceReader {
		t.Fatalf("immutable roleRef replacement = %q, want %q", repaired.RoleRef.Name, namespaceReader)
	}

	var currentSet policyapi.PolicySet
	if err := api.Get(ctx, client.ObjectKeyFromObject(set), &currentSet); err != nil {
		t.Fatal(err)
	}
	currentSet.Spec.Rules = currentSet.Spec.Rules[:1]
	currentSet.Spec.Rules[0].Namespaces = []string{"test-team-a"}
	if err := api.Update(ctx, &currentSet); err != nil {
		t.Fatal(err)
	}
	waitForCachedPolicySet(t, cached, currentSet.Name, 1, 1)
	if _, err := r.Reconcile(ctx, reconcileRequest(binding.Name)); err != nil {
		t.Fatal(err)
	}
	roleBindings, clusterBindings = nativeBindingsFor(t, api, binding.Name)
	if len(roleBindings) != 1 || roleBindings[0].Namespace != "test-team-a" || len(clusterBindings) != 0 {
		t.Fatalf("native bindings after PolicySet revocation: RoleBindings=%v ClusterRoleBindings=%v", roleBindingKeys(roleBindings), clusterBindingKeys(clusterBindings))
	}
	oldName := roleBindings[0].Name

	if err := api.Delete(ctx, account); err != nil {
		t.Fatal(err)
	}
	waitForCachedNotFound(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(account))
	if _, err := r.Reconcile(ctx, reconcileRequest(binding.Name)); err != nil {
		t.Fatal(err)
	}
	if roleBindings, _ := nativeBindingsFor(t, api, binding.Name); len(roleBindings) != 0 {
		t.Fatalf("old subject grants survived missing ServiceAccount: %v", roleBindingKeys(roleBindings))
	}
	newAccount := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: subjectNamespace, Name: account.Name}}
	if err := api.Create(ctx, newAccount); err != nil {
		t.Fatal(err)
	}
	waitForCached(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(newAccount))
	if _, err := r.Reconcile(ctx, reconcileRequest(binding.Name)); err != nil {
		t.Fatal(err)
	}
	roleBindings, _ = nativeBindingsFor(t, api, binding.Name)
	if len(roleBindings) != 1 || roleBindings[0].Name == oldName || roleBindings[0].Annotations[SubjectUIDAnn] != string(newAccount.UID) {
		t.Fatalf("replacement ServiceAccount grant = %+v, UID=%s", roleBindingKeys(roleBindings), newAccount.UID)
	}

	collisionAccount := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: subjectNamespace, Name: "collision-account"}}
	if err := api.Create(ctx, collisionAccount); err != nil {
		t.Fatal(err)
	}
	collisionBinding := testPolicyBinding("collision-grant", collisionAccount.Name, set.Name)
	if err := api.Create(ctx, collisionBinding); err != nil {
		t.Fatal(err)
	}
	waitForCached(t, cached, &corev1.ServiceAccount{}, client.ObjectKeyFromObject(collisionAccount))
	waitForCached(t, cached, &policyapi.PolicyBinding{}, client.ObjectKeyFromObject(collisionBinding))
	var loadedBinding policyapi.PolicyBinding
	if err := api.Get(ctx, client.ObjectKeyFromObject(collisionBinding), &loadedBinding); err != nil {
		t.Fatal(err)
	}
	desired, _, _, err := r.expand(ctx, &loadedBinding)
	if err != nil || len(desired) != 1 {
		t.Fatalf("expand collision binding: desired=%d err=%v", len(desired), err)
	}
	var collided client.Object
	for _, native := range desired {
		collided = native.DeepCopyObject().(client.Object)
		collided.SetLabels(map[string]string{"owner": "foreign"})
		collided.SetAnnotations(map[string]string{"owner": "foreign"})
		if rb, ok := collided.(*rbacv1.RoleBinding); ok {
			rb.Subjects[0].Name = "foreign-account"
		}
		if crb, ok := collided.(*rbacv1.ClusterRoleBinding); ok {
			crb.Subjects[0].Name = "foreign-account"
		}
		break
	}
	if err := api.Create(ctx, collided); err != nil {
		t.Fatal(err)
	}
	waitForCachedNotFound(t, cached, collided, client.ObjectKeyFromObject(collided))
	if _, err := r.Reconcile(ctx, reconcileRequest(collisionBinding.Name)); err == nil {
		t.Fatal("reconcile adopted a foreign native binding collision")
	}
	var untouched rbacv1.RoleBinding
	if err := api.Get(ctx, client.ObjectKeyFromObject(collided), &untouched); err != nil {
		t.Fatal(err)
	}
	if untouched.Labels[ManagedByLabel] == FieldManager || untouched.Subjects[0].Name != "foreign-account" {
		t.Fatalf("foreign binding was changed: %+v", untouched)
	}
	if got := conditionFor(readPolicyBinding(t, api, collisionBinding.Name), KubernetesReady); got == nil || got.Reason != "ApplyFailed" {
		t.Fatalf("collision condition = %+v", got)
	}
	if err := api.Delete(ctx, collided); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, reconcileRequest(collisionBinding.Name)); err != nil {
		t.Fatalf("reconcile after removing collision: %v", err)
	}
}

// TestRoleCreationBindsWhatTheBindingReportedMissing runs the controller with its watches: creating
// each role a binding reported missing reconciles the binding again.
func TestRoleCreationBindsWhatTheBindingReportedMissing(t *testing.T) {
	ctx := context.Background()
	api, _, _ := startPolicyAPIServer(t, true)
	for _, name := range []string{policyNamespace, subjectNamespace, "test-team-a"} {
		if err := api.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}); err != nil {
			t.Fatal(err)
		}
	}
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: subjectNamespace, Name: "test-account"}}
	if err := api.Create(ctx, account); err != nil {
		t.Fatal(err)
	}
	const localReader = "test-local-reader"
	set := &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-late-roles"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{
			{Type: "kubernetes_role_binding", Role: &policyapi.RoleRef{Kind: "Role", Name: localReader}, Namespaces: []string{"test-team-a"}},
			{Type: "kubernetes_cluster_role_binding", ClusterRole: nodeReader},
		}},
	}
	if err := api.Create(ctx, set); err != nil {
		t.Fatal(err)
	}
	binding := testPolicyBinding("test-late-grant", account.Name, set.Name)
	if err := api.Create(ctx, binding); err != nil {
		t.Fatal(err)
	}
	clusterRole := policyapi.MissingRole{Kind: "ClusterRole", Name: nodeReader}
	localRole := policyapi.MissingRole{Namespace: "test-team-a", Kind: "Role", Name: localReader}
	waitForBindingStatus(t, api, binding.Name, metav1.ConditionFalse, []policyapi.MissingRole{clusterRole, localRole})

	if err := api.Create(ctx, &rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Namespace: "test-team-a", Name: localReader}}); err != nil {
		t.Fatal(err)
	}
	waitForBindingStatus(t, api, binding.Name, metav1.ConditionFalse, []policyapi.MissingRole{clusterRole})
	if roleBindings, clusterBindings := nativeBindingsFor(t, api, binding.Name); len(roleBindings) != 1 || len(clusterBindings) != 0 {
		t.Fatalf("native bindings after the Role appeared: RoleBindings=%v ClusterRoleBindings=%v", roleBindingKeys(roleBindings), clusterBindingKeys(clusterBindings))
	}

	if err := api.Create(ctx, &rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: nodeReader}}); err != nil {
		t.Fatal(err)
	}
	waitForBindingStatus(t, api, binding.Name, metav1.ConditionTrue, nil)
	if roleBindings, clusterBindings := nativeBindingsFor(t, api, binding.Name); len(roleBindings) != 1 || len(clusterBindings) != 1 {
		t.Fatalf("native bindings after the ClusterRole appeared: RoleBindings=%v ClusterRoleBindings=%v", roleBindingKeys(roleBindings), clusterBindingKeys(clusterBindings))
	}
}

func waitForBindingStatus(t *testing.T, c client.Client, name string, ready metav1.ConditionStatus, missingRoles []policyapi.MissingRole) {
	t.Helper()
	var binding *policyapi.PolicyBinding
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		binding = readPolicyBinding(t, c, name)
		if condition := conditionFor(binding, KubernetesReady); condition != nil && condition.Status == ready && slices.Equal(binding.Status.MissingRoles, missingRoles) {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("PolicyBinding %s status = %+v, want KubernetesReady %s and missingRoles %+v", name, binding.Status, ready, missingRoles)
}

// startPolicyAPIServer returns a direct client, the manager's cached client and a Reconciler over
// it. With controller, the manager runs the Reconciler with its watches; otherwise tests call
// Reconcile themselves.
func startPolicyAPIServer(t *testing.T, controller bool) (client.Client, client.Client, *Reconciler) {
	t.Helper()
	kube, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := kube.Stop(); err != nil {
			t.Error(err)
		}
	})
	config := kube.Config

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	api, err := client.New(config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	managed := labels.Set(map[string]string{ManagedByLabel: FieldManager, PolicyNamespaceLbl: policyNamespace}).AsSelector()
	manager, err := ctrl.NewManager(config, ctrl.Options{
		Scheme:                 scheme,
		Metrics:                server.Options{BindAddress: "0"},
		HealthProbeBindAddress: "0",
		WebhookServer:          webhook.NewServer(webhook.Options{Port: 0}),
		Cache: cache.Options{ByObject: map[client.Object]cache.ByObject{
			&policyapi.PolicySet{}:       {Namespaces: map[string]cache.Config{policyNamespace: {}}},
			&policyapi.PolicyBinding{}:   {Namespaces: map[string]cache.Config{policyNamespace: {}}},
			&corev1.ServiceAccount{}:     {Namespaces: map[string]cache.Config{subjectNamespace: {}}},
			&rbacv1.RoleBinding{}:        {Label: managed},
			&rbacv1.ClusterRoleBinding{}: {Label: managed},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	r := &Reconciler{
		Client:            manager.GetClient(),
		APIReader:         manager.GetAPIReader(),
		PolicyNamespace:   policyNamespace,
		SubjectNamespaces: []string{subjectNamespace},
	}
	if controller {
		if err := r.SetupWithManager(manager); err != nil {
			t.Fatal(err)
		}
	} else {
		indexNativeBinding := func(obj client.Object) []string { return bindingIndex(obj) }
		for _, object := range []client.Object{&rbacv1.RoleBinding{}, &rbacv1.ClusterRoleBinding{}} {
			if err := manager.GetFieldIndexer().IndexField(context.Background(), object, nativeBindingIndex, indexNativeBinding); err != nil {
				t.Fatal(err)
			}
		}
	}
	managerCtx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- manager.Start(managerCtx) }()
	t.Cleanup(func() {
		cancel()
		if err := <-done; err != nil {
			t.Errorf("stop controller manager: %v", err)
		}
	})
	syncCtx, syncCancel := context.WithTimeout(managerCtx, 30*time.Second)
	defer syncCancel()
	if !manager.GetCache().WaitForCacheSync(syncCtx) {
		t.Fatal("controller cache did not sync")
	}
	return api, manager.GetClient(), r
}

func testPolicySet(name string, namespaces []string, includeClusterRole bool) *policyapi.PolicySet {
	rules := []policyapi.Rule{{
		Type:       "kubernetes_role_binding",
		Role:       &policyapi.RoleRef{Kind: "ClusterRole", Name: namespaceReader},
		Namespaces: slices.Clone(namespaces),
	}}
	if includeClusterRole {
		rules = append(rules, policyapi.Rule{Type: "kubernetes_cluster_role_binding", ClusterRole: nodeReader})
	}
	return &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: name},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	}
}

func testPolicyBinding(name, account, set string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: name},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.ServiceAccountRef{Namespace: subjectNamespace, Name: account},
			PolicySets: []string{set},
		},
	}
}

func reconcileRequest(name string) reconcile.Request {
	return reconcile.Request{NamespacedName: client.ObjectKey{Namespace: policyNamespace, Name: name}}
}

func waitForCached(t *testing.T, c client.Client, object client.Object, key client.ObjectKey) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		candidate := object.DeepCopyObject().(client.Object)
		if err := c.Get(context.Background(), key, candidate); err == nil {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("%T %s did not appear in manager cache", object, key)
}

func waitForCachedNotFound(t *testing.T, c client.Client, object client.Object, key client.ObjectKey) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		candidate := object.DeepCopyObject().(client.Object)
		if err := c.Get(context.Background(), key, candidate); apierrors.IsNotFound(err) {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("%T %s remained in manager cache", object, key)
}

func waitForCachedSubjectName(t *testing.T, c client.Client, object client.Object, subject string) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		candidate := object.DeepCopyObject().(client.Object)
		if err := c.Get(context.Background(), client.ObjectKeyFromObject(object), candidate); err == nil &&
			slices.ContainsFunc(nativeSubjects(candidate), func(current rbacv1.Subject) bool { return current.Name == subject }) {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("%T %s did not show subject %s in manager cache", object, client.ObjectKeyFromObject(object), subject)
}

func waitForCachedRoleRef(t *testing.T, c client.Client, namespace, name, role string) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		var binding rbacv1.RoleBinding
		if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: name}, &binding); err == nil && binding.RoleRef.Name == role {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("RoleBinding %s/%s did not show roleRef %s in manager cache", namespace, name, role)
}

func waitForCachedPolicySet(t *testing.T, c client.Client, name string, rules, namespaces int) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		var set policyapi.PolicySet
		if err := c.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: name}, &set); err == nil && len(set.Spec.Rules) == rules && len(set.Spec.Rules[0].Namespaces) == namespaces {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("PolicySet %s did not show %d rules with %d namespaces in manager cache", name, rules, namespaces)
}

func waitForCachedCondition(t *testing.T, c client.Client, name, conditionType string) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		var binding policyapi.PolicyBinding
		if err := c.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: name}, &binding); err == nil && conditionFor(&binding, conditionType) != nil {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("PolicyBinding %s did not show condition %s in manager cache", name, conditionType)
}

func nativeBindingsFor(t *testing.T, c client.Client, bindingName string) ([]*rbacv1.RoleBinding, []*rbacv1.ClusterRoleBinding) {
	t.Helper()
	var roleBindings rbacv1.RoleBindingList
	if err := c.List(context.Background(), &roleBindings); err != nil {
		t.Fatal(err)
	}
	var clusterBindings rbacv1.ClusterRoleBindingList
	if err := c.List(context.Background(), &clusterBindings); err != nil {
		t.Fatal(err)
	}
	var roles []*rbacv1.RoleBinding
	for i := range roleBindings.Items {
		if roleBindings.Items[i].Annotations[BindingAnnotation] == bindingName {
			roles = append(roles, &roleBindings.Items[i])
		}
	}
	var clusters []*rbacv1.ClusterRoleBinding
	for i := range clusterBindings.Items {
		if clusterBindings.Items[i].Annotations[BindingAnnotation] == bindingName {
			clusters = append(clusters, &clusterBindings.Items[i])
		}
	}
	return roles, clusters
}

func nativeObjects(roles []*rbacv1.RoleBinding, clusters []*rbacv1.ClusterRoleBinding) []client.Object {
	objects := make([]client.Object, 0, len(roles)+len(clusters))
	for _, object := range roles {
		objects = append(objects, object)
	}
	for _, object := range clusters {
		objects = append(objects, object)
	}
	return objects
}

func hasApplyManager(managedFields []metav1.ManagedFieldsEntry) bool {
	return slices.ContainsFunc(managedFields, func(entry metav1.ManagedFieldsEntry) bool {
		return entry.Manager == FieldManager && entry.Operation == metav1.ManagedFieldsOperationApply
	})
}

func managedFields(obj client.Object) []metav1.ManagedFieldsEntry {
	switch object := obj.(type) {
	case *rbacv1.RoleBinding:
		return object.ManagedFields
	case *rbacv1.ClusterRoleBinding:
		return object.ManagedFields
	default:
		return nil
	}
}

func readPolicyBinding(t *testing.T, c client.Client, name string) *policyapi.PolicyBinding {
	t.Helper()
	var binding policyapi.PolicyBinding
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: policyNamespace, Name: name}, &binding); err != nil {
		t.Fatal(err)
	}
	return &binding
}

func conditionFor(binding *policyapi.PolicyBinding, name string) *metav1.Condition {
	for i := range binding.Status.Conditions {
		if binding.Status.Conditions[i].Type == name {
			return &binding.Status.Conditions[i]
		}
	}
	return nil
}

func nativeSubjects(obj client.Object) []rbacv1.Subject {
	if roleBinding, ok := obj.(*rbacv1.RoleBinding); ok {
		return roleBinding.Subjects
	}
	return obj.(*rbacv1.ClusterRoleBinding).Subjects
}

func roleBindingKeys(bindings []*rbacv1.RoleBinding) []string {
	keys := make([]string, 0, len(bindings))
	for _, binding := range bindings {
		keys = append(keys, fmt.Sprintf("%s/%s", binding.Namespace, binding.Name))
	}
	return keys
}

func clusterBindingKeys(bindings []*rbacv1.ClusterRoleBinding) []string {
	keys := make([]string, 0, len(bindings))
	for _, binding := range bindings {
		keys = append(keys, binding.Name)
	}
	return keys
}
