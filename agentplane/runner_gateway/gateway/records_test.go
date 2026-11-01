package gateway

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	coordinationv1 "k8s.io/api/coordination/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	kubernetesfake "k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

const (
	testNamespace  = "test-sandboxes"
	testSandboxUID = "test-sandbox-uid-a"
	testPodUID     = "test-runner-pod-uid-a"
)

func newKubernetesFake(objects ...runtime.Object) *kubernetesfake.Clientset {
	kube := kubernetesfake.NewSimpleClientset(objects...)
	var nextLeaseUID int
	kube.PrependReactor("create", "leases", func(action k8stesting.Action) (bool, runtime.Object, error) {
		lease := action.(k8stesting.CreateAction).GetObject().(*coordinationv1.Lease)
		if lease.UID == "" {
			nextLeaseUID++
			lease.UID = types.UID(fmt.Sprintf("fixture-lease-%d", nextLeaseUID))
		}
		return false, nil, nil
	})
	return kube
}

func TestConnectionRecordsRejectForeignObjects(t *testing.T) {
	sandbox := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
	owner := sandboxOwnerReference(sandbox)
	cases := []struct {
		name   string
		mutate func(*metav1.ObjectMeta)
	}{
		{"unowned", func(m *metav1.ObjectMeta) { m.OwnerReferences = nil }},
		{"previous Sandbox", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].UID = "previous-sandbox-uid" }},
		{"different API", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].APIVersion = "other.example/v1" }},
		{"different kind", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].Kind = "Other" }},
		{"different name", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].Name = "other-sandbox" }},
		{"multiple owners", func(m *metav1.ObjectMeta) { m.OwnerReferences = append(m.OwnerReferences, owner) }},
		{"deleting", func(m *metav1.ObjectMeta) { m.DeletionTimestamp = &metav1.Time{Time: time.Now()} }},
	}
	for _, kind := range []string{"Lease", "EndpointSlice"} {
		for _, test := range cases {
			t.Run(kind+"/"+test.name, func(t *testing.T) {
				metadata := metav1.ObjectMeta{Namespace: testNamespace, OwnerReferences: []metav1.OwnerReference{owner}}
				test.mutate(&metadata)
				var object runtime.Object
				if kind == "Lease" {
					metadata.Name = sandbox.Name + sandboxlease.RunnerLeaseSuffix
					object = &coordinationv1.Lease{ObjectMeta: metadata}
				} else {
					metadata.Name = sandbox.Name + sandboxapi.ControlServiceSuffix
					object = &discoveryv1.EndpointSlice{ObjectMeta: metadata}
				}
				objects := []runtime.Object{object}
				leaseUID := types.UID("fixture-lease")
				if kind == "EndpointSlice" {
					objects = append(objects, &coordinationv1.Lease{
						ObjectMeta: metav1.ObjectMeta{
							Name: sandbox.Name + sandboxlease.RunnerLeaseSuffix, Namespace: testNamespace,
							UID: leaseUID, OwnerReferences: []metav1.OwnerReference{owner},
						},
						Spec: coordinationv1.LeaseSpec{HolderIdentity: stringPointer("gateway"), LeaseTransitions: int32Pointer(1)},
					})
				}
				kube := newKubernetesFake(objects...)
				records, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
					connectionHolder{name: "gateway", ip: "10.0.0.1", port: sandboxapi.ControlServicePort})
				if err != nil {
					t.Fatal(err)
				}
				if kind == "Lease" {
					_, err = records.claim(context.Background(), sandbox, testPodUID)
				} else {
					err = records.pointControlEndpoints(context.Background(), sandbox, leaseUID, 1)
				}
				if err == nil || !strings.Contains(err.Error(), "refusing") {
					t.Fatalf("expected ownership refusal, got %v", err)
				}
				for _, action := range kube.Actions() {
					if action.GetVerb() != "get" {
						t.Fatalf("refusal mutated Kubernetes state: %s %s", action.GetVerb(), action.GetResource().Resource)
					}
				}
			})
		}
	}
}

// Another replica's claim expires one Lease duration after this replica last saw it renewed, whatever its
// writer's clock put in RenewTime: a clock behind must not expire a live claim early, nor one ahead keep a
// dead claim for the length of the skew.
func TestClaimExpiresByLocallyObservedRenewal(t *testing.T) {
	for _, skew := range []time.Duration{-time.Hour, time.Hour} {
		t.Run(skew.String(), func(t *testing.T) {
			ctx := context.Background()
			identity := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
			start := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
			writerClock := start.Add(skew)
			kube := newKubernetesFake(&coordinationv1.Lease{
				ObjectMeta: metav1.ObjectMeta{
					Name: testSandbox + sandboxlease.RunnerLeaseSuffix, Namespace: testNamespace,
					UID:             "fixture-existing-lease",
					OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(identity)},
				},
				Spec: coordinationv1.LeaseSpec{
					HolderIdentity: stringPointer("test-other-gateway"), LeaseDurationSeconds: int32Pointer(leaseDurationSeconds),
					LeaseTransitions: int32Pointer(1), RenewTime: &metav1.MicroTime{Time: writerClock},
				},
			})
			records, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
				connectionHolder{name: "test-gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
			if err != nil {
				t.Fatal(err)
			}
			now := start
			records.now = func() time.Time { return now }
			duration := time.Duration(leaseDurationSeconds) * time.Second
			refused := func(at time.Duration) {
				t.Helper()
				now = start.Add(at)
				if _, err := records.claim(ctx, identity, testPodUID); !errors.Is(err, errRunnerOwned) {
					t.Fatalf("claim at %v took a live connection: %v", at, err)
				}
			}
			refused(0)
			refused(duration - time.Second)
			// The holder renews by its own clock, which the claim never compares with this one.
			renewedAt := duration - time.Second
			lease, err := kube.CoordinationV1().Leases(testNamespace).Get(ctx, testSandbox+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			lease.Spec.RenewTime = &metav1.MicroTime{Time: writerClock.Add(renewedAt)}
			if _, err := kube.CoordinationV1().Leases(testNamespace).Update(ctx, lease, metav1.UpdateOptions{}); err != nil {
				t.Fatal(err)
			}
			// Expiry counts from when this replica first sees the renewal, not when it was written.
			seenAt := renewedAt + time.Second
			refused(seenAt)
			refused(seenAt + duration - time.Second)
			now = start.Add(seenAt + duration)
			epoch, err := records.claim(ctx, identity, testPodUID)
			if err != nil {
				t.Fatalf("lapsed connection was not taken over: %v", err)
			}
			if epoch != 2 {
				t.Fatalf("takeover epoch = %d, want 2", epoch)
			}
		})
	}
}

func TestConnectionRenewPublishesAndClearsAutosuspendBlockers(t *testing.T) {
	kube := newKubernetesFake()
	identity := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
	records, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
		connectionHolder{name: "test-gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
	if err != nil {
		t.Fatal(err)
	}
	epoch, err := records.claim(context.Background(), identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	sessionID := "session-a"
	busy := &sandboxlease.RunnerFacts{AutosuspendBlockers: []sandboxapi.AutosuspendBlocker{{
		Reason: sandboxapi.AutosuspendBlockerActiveTurn, SessionID: &sessionID,
	}}}
	if err := records.renew(context.Background(), identity, epoch, time.Now(), busy); err != nil {
		t.Fatal(err)
	}
	name := testSandbox + sandboxlease.RunnerLeaseSuffix
	lease, err := kube.CoordinationV1().Leases(testNamespace).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.AutosuspendBlockersAnnotation] == "" {
		t.Fatal("busy runner facts were not published on the connection Lease")
	}
	if err := records.renew(context.Background(), identity, epoch, time.Now(), &sandboxlease.RunnerFacts{}); err != nil {
		t.Fatal(err)
	}
	lease, err = kube.CoordinationV1().Leases(testNamespace).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if _, exists := lease.Annotations[sandboxlease.AutosuspendBlockersAnnotation]; exists {
		t.Fatalf("idle runner retained blocker annotation: %s", lease.Annotations[sandboxlease.AutosuspendBlockersAnnotation])
	}
}

func TestObservationClaimsPreserveVerifiedContact(t *testing.T) {
	ctx := context.Background()
	kube := newKubernetesFake()
	identity := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
	records, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
		connectionHolder{name: "gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
	if err != nil {
		t.Fatal(err)
	}
	epoch, err := records.claim(ctx, identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	read := func() *coordinationv1.Lease {
		t.Helper()
		lease, err := kube.CoordinationV1().Leases(testNamespace).Get(ctx, testSandbox+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		return lease
	}
	if sandboxlease.LastConnectedPodUID(read().Annotations) != "" {
		t.Fatal("claim counted as contact")
	}
	if err := records.recordBootID(ctx, identity, epoch, "boot"); err != nil {
		t.Fatal(err)
	}
	for _, uid := range []string{testPodUID, "replacement-pod"} {
		epoch, err = records.claim(ctx, identity, uid)
		if err != nil {
			t.Fatal(err)
		}
		lease := read()
		if lease.Annotations[sandboxlease.RunnerBootIDAnnotation] != "" {
			t.Fatal("claim retained current boot ID")
		}
		if sandboxlease.LastConnectedPodUID(lease.Annotations) != testPodUID {
			t.Fatal("claim erased verified contact")
		}
	}
	if err := records.recordBootID(ctx, identity, epoch, "replacement-boot"); err != nil {
		t.Fatal(err)
	}
	if sandboxlease.LastConnectedPodUID(read().Annotations) != "replacement-pod" {
		t.Fatal("verified replacement did not update contact")
	}
}

func TestControlRouteRecoversAfterLeaseRecreation(t *testing.T) {
	ctx := context.Background()
	identity := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
	oldLeaseUID := types.UID("fixture-old-lease")
	oldLease := &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Name: testSandbox + sandboxlease.RunnerLeaseSuffix, Namespace: testNamespace,
			UID: oldLeaseUID, OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(identity)},
		},
		Spec: coordinationv1.LeaseSpec{
			HolderIdentity: stringPointer("test-old-gateway"), LeaseTransitions: int32Pointer(9),
		},
	}
	route := &discoveryv1.EndpointSlice{
		ObjectMeta: metav1.ObjectMeta{
			Name: testSandbox + sandboxapi.ControlServiceSuffix, Namespace: testNamespace,
			OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(identity)},
			Annotations: map[string]string{
				sandboxlease.ConnectionEpochAnnotation:    "9",
				sandboxlease.ConnectionLeaseUIDAnnotation: string(oldLeaseUID),
			},
		},
		Endpoints: []discoveryv1.Endpoint{{Addresses: []string{"192.0.2.40"}}},
	}
	kube := newKubernetesFake(oldLease, route)
	if err := kube.CoordinationV1().Leases(testNamespace).Delete(ctx, oldLease.Name, metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	current, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
		connectionHolder{name: "test-new-gateway", ip: "192.0.2.41", port: sandboxapi.ControlServicePort})
	if err != nil {
		t.Fatal(err)
	}
	epoch, err := current.claim(ctx, identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	if epoch != 10 {
		t.Fatalf("recreated Lease epoch = %d, want 10 above the retained route", epoch)
	}
	lease, err := kube.CoordinationV1().Leases(testNamespace).Get(ctx, oldLease.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.UID == oldLeaseUID || *lease.Spec.LeaseTransitions != 10 {
		t.Fatalf("recreated Lease = UID %q, transitions %d", lease.UID, *lease.Spec.LeaseTransitions)
	}
	if err := current.pointControlEndpoints(ctx, identity, oldLeaseUID, 9); !errors.Is(err, errSupersededConnection) {
		t.Fatalf("old Lease publication error = %v, want superseded", err)
	}
	updated, err := kube.DiscoveryV1().EndpointSlices(testNamespace).Get(ctx, route.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if updated.Annotations[sandboxlease.ConnectionEpochAnnotation] != "10" ||
		updated.Annotations[sandboxlease.ConnectionLeaseUIDAnnotation] != string(lease.UID) ||
		len(updated.Endpoints) != 1 || updated.Endpoints[0].Addresses[0] != "192.0.2.41" {
		t.Fatalf("control route after Lease recreation = %+v", updated)
	}
}

func TestControlRouteEpochUsesLegacyBaselineAndRejectsInvalidValues(t *testing.T) {
	identity := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
	cases := []struct {
		name       string
		annotation string
		wantEpoch  uint64
		wantError  bool
	}{
		{name: "legacy absent annotation", wantEpoch: 0},
		{name: "malformed", annotation: "not-an-epoch", wantError: true},
		{name: "overflow", annotation: "2147483648", wantError: true},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			route := &discoveryv1.EndpointSlice{ObjectMeta: metav1.ObjectMeta{
				Name: testSandbox + sandboxapi.ControlServiceSuffix, Namespace: testNamespace,
				OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(identity)},
				Annotations:     map[string]string{sandboxlease.ConnectionEpochAnnotation: test.annotation},
			}}
			kube := newKubernetesFake(route)
			records, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
				connectionHolder{name: "gateway", ip: "192.0.2.60", port: sandboxapi.ControlServicePort})
			if err != nil {
				t.Fatal(err)
			}
			epoch, err := records.controlRouteEpoch(context.Background(), identity)
			if test.wantError {
				if err == nil {
					t.Fatal("expected invalid epoch to fail closed")
				}
				return
			}
			if err != nil || epoch != test.wantEpoch {
				t.Fatalf("controlRouteEpoch() = (%d, %v), want (%d, nil)", epoch, err, test.wantEpoch)
			}
		})
	}
}

func TestDelayedRoutePublicationCannotRegressRecreatedLeaseRoute(t *testing.T) {
	ctx := context.Background()
	identity := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
	oldLeaseUID := types.UID("fixture-old-lease")
	oldLease := &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Name: testSandbox + sandboxlease.RunnerLeaseSuffix, Namespace: testNamespace,
			UID: oldLeaseUID, OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(identity)},
		},
		Spec: coordinationv1.LeaseSpec{
			HolderIdentity: stringPointer("test-old-gateway"), LeaseTransitions: int32Pointer(2),
		},
	}
	route := &discoveryv1.EndpointSlice{
		ObjectMeta: metav1.ObjectMeta{
			Name: testSandbox + sandboxapi.ControlServiceSuffix, Namespace: testNamespace,
			OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(identity)},
			Annotations: map[string]string{
				sandboxlease.ConnectionEpochAnnotation:    "1",
				sandboxlease.ConnectionLeaseUIDAnnotation: string(oldLeaseUID),
			},
		},
		Endpoints: []discoveryv1.Endpoint{{Addresses: []string{"192.0.2.50"}}},
	}
	kube := newKubernetesFake(oldLease, route)
	oldRecords, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
		connectionHolder{name: "test-old-gateway", ip: "192.0.2.50", port: sandboxapi.ControlServicePort})
	if err != nil {
		t.Fatal(err)
	}
	newLease := &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Name: oldLease.Name, Namespace: testNamespace, UID: "fixture-new-lease",
			OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(identity)},
		},
		Spec: coordinationv1.LeaseSpec{
			HolderIdentity: stringPointer("test-new-gateway"), LeaseTransitions: int32Pointer(2),
		},
	}
	newRoute := route.DeepCopy()
	newRoute.Annotations[sandboxlease.ConnectionEpochAnnotation] = "2"
	newRoute.Annotations[sandboxlease.ConnectionLeaseUIDAnnotation] = string(newLease.UID)
	newRoute.Endpoints[0].Addresses[0] = "192.0.2.51"
	transitioned := false
	kube.PrependReactor("update", "endpointslices", func(action k8stesting.Action) (bool, runtime.Object, error) {
		updated := action.(k8stesting.UpdateAction).GetObject().(*discoveryv1.EndpointSlice)
		if updated.Endpoints[0].Addresses[0] != "192.0.2.50" {
			return false, nil, nil
		}
		transitioned = true
		tracker := kube.Tracker()
		if err := tracker.Delete(coordinationv1.SchemeGroupVersion.WithResource("leases"), testNamespace, oldLease.Name); err != nil {
			t.Fatalf("delete old Lease from fake tracker: %v", err)
		}
		if err := tracker.Add(newLease); err != nil {
			t.Fatalf("add replacement Lease to fake tracker: %v", err)
		}
		if err := tracker.Update(discoveryv1.SchemeGroupVersion.WithResource("endpointslices"), newRoute, testNamespace); err != nil {
			t.Fatalf("write replacement route to fake tracker: %v", err)
		}
		return true, nil, apierrors.NewConflict(
			discoveryv1.Resource("endpointslices"), updated.Name, errors.New("route changed while publication was delayed"),
		)
	})
	if err := oldRecords.pointControlEndpoints(ctx, identity, oldLeaseUID, 2); !errors.Is(err, errSupersededConnection) {
		t.Fatalf("delayed old publication error = %v, want superseded", err)
	}
	if !transitioned {
		t.Fatal("old route did not reach the conflict reactor")
	}
	updated, err := kube.DiscoveryV1().EndpointSlices(testNamespace).Get(ctx, route.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if updated.Annotations[sandboxlease.ConnectionLeaseUIDAnnotation] == string(oldLeaseUID) ||
		updated.Annotations[sandboxlease.ConnectionEpochAnnotation] != "2" ||
		updated.Endpoints[0].Addresses[0] != "192.0.2.51" {
		t.Fatalf("delayed old publication regressed control route: %+v", updated)
	}
}
