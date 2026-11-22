package main

import (
	"context"
	"slices"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
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

func reconcileNode(t *testing.T, c client.Client, name string) (*corev1.Node, reconcile.Result) {
	t.Helper()
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
			c := fake.NewClientBuilder().WithObjects(tc.objects...).Build()
			got, result := reconcileNode(t, c, name)
			if tainted := hasTaint(got.Spec.Taints, outOfServiceTaint.Key); tainted != tc.wantTainted {
				t.Errorf("out-of-service taint present = %v, want %v (taints %v)", tainted, tc.wantTainted, got.Spec.Taints)
			}
			if result.RequeueAfter != tc.wantRequeue {
				t.Errorf("RequeueAfter = %v, want %v", result.RequeueAfter, tc.wantRequeue)
			}
		})
	}
}

func TestOutOfServiceTaintAcrossLeaseRenewal(t *testing.T) {
	unreachable := corev1.Taint{Key: corev1.TaintNodeUnreachable, Effect: corev1.TaintEffectNoExecute}
	laptop := node("laptop", corev1.ConditionUnknown, roamingTaint, unreachable)
	heartbeat := lease(laptop.Name, time.Hour)
	c := fake.NewClientBuilder().WithObjects(laptop, heartbeat).Build()

	got, result := reconcileNode(t, c, laptop.Name)
	index := slices.IndexFunc(got.Spec.Taints, func(t corev1.Taint) bool { return t.Key == corev1.TaintNodeOutOfService })
	if index < 0 {
		t.Fatalf("stale node missing out-of-service taint: %v", got.Spec.Taints)
	}
	if taint := got.Spec.Taints[index]; taint.Effect != corev1.TaintEffectNoExecute || taint.TimeAdded == nil || result.RequeueAfter != 0 {
		t.Fatalf("stale node taint = %+v, requeue = %v", taint, result.RequeueAfter)
	}

	renewed := metav1.NewMicroTime(now)
	heartbeat.Spec.RenewTime = &renewed
	if err := c.Update(context.Background(), heartbeat); err != nil {
		t.Fatal(err)
	}
	got, result = reconcileNode(t, c, laptop.Name)
	if len(got.Spec.Taints) != 2 || !slices.Contains(got.Spec.Taints, roamingTaint) || !slices.Contains(got.Spec.Taints, unreachable) || result.RequeueAfter != 0 {
		t.Errorf("after renewal taints = %v, requeue = %v", got.Spec.Taints, result.RequeueAfter)
	}
}
