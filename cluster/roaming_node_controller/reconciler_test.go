package main

import (
	"context"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

var (
	now          = time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC)
	roamingTaint = corev1.Taint{Key: roamingTaintKey, Value: "true", Effect: corev1.TaintEffectNoSchedule}
)

func node(name string, ready corev1.ConditionStatus, taints ...corev1.Taint) *corev1.Node {
	return &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec:       corev1.NodeSpec{Taints: taints},
		Status: corev1.NodeStatus{
			Conditions: []corev1.NodeCondition{{Type: corev1.NodeReady, Status: ready}},
		},
	}
}

func lease(name string, renewedAgo time.Duration) *coordinationv1.Lease {
	renewed := metav1.NewMicroTime(now.Add(-renewedAgo))
	duration := int32(40)
	return &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{Namespace: nodeLeaseNamespace, Name: name},
		Spec:       coordinationv1.LeaseSpec{RenewTime: &renewed, LeaseDurationSeconds: &duration},
	}
}

func reconcileNode(t *testing.T, name string, objects ...client.Object) (*corev1.Node, reconcile.Result) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
	r := &Reconciler{Client: c, Now: func() time.Time { return now }}
	result, err := r.Reconcile(context.Background(), reconcile.Request{NamespacedName: types.NamespacedName{Name: name}})
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	var got corev1.Node
	if err := c.Get(context.Background(), types.NamespacedName{Name: name}, &got); err != nil {
		t.Fatal(err)
	}
	return &got, result
}

func TestReconcile(t *testing.T) {
	for _, tc := range []struct {
		name        string
		objects     []client.Object
		wantTainted bool
		// Zero means no timed requeue: a Lease or Node event is what wakes the node next.
		wantRequeue time.Duration
	}{
		{
			name:        "roaming kubelet silent past the threshold is marked out of service",
			objects:     []client.Object{node("laptop", corev1.ConditionUnknown, roamingTaint), lease("laptop", 6*time.Minute)},
			wantTainted: true,
		},
		{
			name:        "roaming node whose kubelet never renewed a Lease is marked out of service",
			objects:     []client.Object{node("laptop", corev1.ConditionUnknown, roamingTaint)},
			wantTainted: true,
		},
		{
			name:        "silence below the threshold waits until it would be reached",
			objects:     []client.Object{node("laptop", corev1.ConditionUnknown, roamingTaint), lease("laptop", 4*time.Minute)},
			wantRequeue: time.Minute,
		},
		{
			name:        "stale Lease on a node still reported Ready waits for the lifecycle controller",
			objects:     []client.Object{node("laptop", corev1.ConditionTrue, roamingTaint), lease("laptop", 6*time.Minute)},
			wantRequeue: readyRecheck,
		},
		{
			name:    "a renewed Lease removes the taint at once",
			objects: []client.Object{node("laptop", corev1.ConditionUnknown, roamingTaint, outOfServiceTaint), lease("laptop", time.Second)},
		},
		{
			name:        "a Lease renewed minutes ago leaves the taint (hysteresis)",
			objects:     []client.Object{node("laptop", corev1.ConditionUnknown, roamingTaint, outOfServiceTaint), lease("laptop", 2*time.Minute)},
			wantTainted: true,
		},
		{
			name:    "a node without the roaming taint is never marked",
			objects: []client.Object{node("server", corev1.ConditionUnknown), lease("server", time.Hour)},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			name := tc.objects[0].GetName()
			got, result := reconcileNode(t, name, tc.objects...)
			if tainted := hasTaint(got.Spec.Taints, outOfServiceTaint.Key); tainted != tc.wantTainted {
				t.Errorf("out-of-service taint present = %v, want %v (taints %v)", tainted, tc.wantTainted, got.Spec.Taints)
			}
			if result.RequeueAfter != tc.wantRequeue {
				t.Errorf("RequeueAfter = %v, want %v", result.RequeueAfter, tc.wantRequeue)
			}
		})
	}
}

func TestOutOfServiceTaintKeepsOtherTaints(t *testing.T) {
	unreachable := corev1.Taint{Key: corev1.TaintNodeUnreachable, Effect: corev1.TaintEffectNoExecute}
	got, _ := reconcileNode(t, "laptop", node("laptop", corev1.ConditionUnknown, roamingTaint, unreachable), lease("laptop", time.Hour))
	if !hasTaint(got.Spec.Taints, roamingTaintKey) || !hasTaint(got.Spec.Taints, corev1.TaintNodeUnreachable) {
		t.Errorf("existing taints dropped: %v", got.Spec.Taints)
	}
	for _, taint := range got.Spec.Taints {
		if taint.Key == outOfServiceTaint.Key && (taint.Effect != corev1.TaintEffectNoExecute || taint.TimeAdded == nil) {
			t.Errorf("out-of-service taint = %+v, want NoExecute with TimeAdded", taint)
		}
	}

	back := corev1.Taint{Key: outOfServiceTaint.Key, Value: outOfServiceTaint.Value, Effect: outOfServiceTaint.Effect}
	got, _ = reconcileNode(t, "laptop", node("laptop", corev1.ConditionUnknown, roamingTaint, unreachable, back), lease("laptop", 0))
	if hasTaint(got.Spec.Taints, outOfServiceTaint.Key) || len(got.Spec.Taints) != 2 {
		t.Errorf("untaint should drop only out-of-service: %v", got.Spec.Taints)
	}
}
