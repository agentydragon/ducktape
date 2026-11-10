package gateway

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"strings"
	"testing"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	discoveryclient "k8s.io/client-go/kubernetes/typed/discovery/v1"
)

const (
	testSandboxUID = "test-sandbox-uid-a"
	testPodUID     = "test-runner-pod-uid-a"
	// testFinalizer holds an object in deletion for as long as a test needs it there.
	testFinalizer = "agentplane.allegedly.works/test-keep"
)

// recordsFixture is a namespace of the test's own on the shared API server, holding one Sandbox's connection
// records. Nothing runs a garbage collector, so the owner references name a Sandbox no one created.
type recordsFixture struct {
	t         *testing.T
	namespace string
	identity  *sandboxreader.Identity
}

func newRecordsFixture(t *testing.T) recordsFixture {
	t.Helper()
	f := recordsFixture{
		t: t, namespace: fmt.Sprintf("test-records-%d", namespaces.Add(1)),
		identity: &sandboxreader.Identity{Name: testSandbox, Uid: testSandboxUID},
	}
	namespace := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: f.namespace}}
	if _, err := api.Clientset.CoreV1().Namespaces().Create(t.Context(), namespace, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	return f
}

func (f recordsFixture) leases() coordinationclient.LeaseInterface {
	return api.Clientset.CoordinationV1().Leases(f.namespace)
}

func (f recordsFixture) endpointSlices() discoveryclient.EndpointSliceInterface {
	return api.Clientset.DiscoveryV1().EndpointSlices(f.namespace)
}

func (f recordsFixture) records(holder connectionHolder) *connectionRecords {
	return f.recordsWith(api.Clientset.CoordinationV1(), api.Clientset.DiscoveryV1(), holder)
}

func (f recordsFixture) recordsWith(
	coordination coordinationclient.CoordinationV1Interface, discovery discoveryclient.DiscoveryV1Interface, holder connectionHolder,
) *connectionRecords {
	f.t.Helper()
	records, err := newConnectionRecords(coordination, discovery, f.namespace, holder)
	if err != nil {
		f.t.Fatal(err)
	}
	return records
}

func (f recordsFixture) leaseName() string {
	return f.identity.Name + sandboxlease.EnvironmentRuntimeLeaseSuffix
}

func (f recordsFixture) routeName() string { return f.identity.Name + sandboxapi.ControlServiceSuffix }

// createLease stores lease, filling in its namespace, name and owner when unset.
func (f recordsFixture) createLease(lease *coordinationv1.Lease) *coordinationv1.Lease {
	f.t.Helper()
	lease.Namespace = f.namespace
	if lease.Name == "" {
		lease.Name = f.leaseName()
	}
	if lease.OwnerReferences == nil {
		lease.OwnerReferences = []metav1.OwnerReference{sandboxOwnerReference(f.identity)}
	}
	created, err := f.leases().Create(f.t.Context(), lease, metav1.CreateOptions{})
	if err != nil {
		f.t.Fatal(err)
	}
	return created
}

// route is a control route to address, as a Gateway publishes it; its epoch annotations are the caller's.
func (f recordsFixture) route(address string, annotations map[string]string) *discoveryv1.EndpointSlice {
	return &discoveryv1.EndpointSlice{
		ObjectMeta: metav1.ObjectMeta{
			Name: f.routeName(), Namespace: f.namespace, Annotations: annotations,
			OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(f.identity)},
		},
		AddressType: discoveryv1.AddressTypeIPv4,
		Endpoints:   []discoveryv1.Endpoint{{Addresses: []string{address}}},
	}
}

func (f recordsFixture) createRoute(route *discoveryv1.EndpointSlice) *discoveryv1.EndpointSlice {
	f.t.Helper()
	created, err := f.endpointSlices().Create(f.t.Context(), route, metav1.CreateOptions{})
	if err != nil {
		f.t.Fatal(err)
	}
	return created
}

func (f recordsFixture) readLease() *coordinationv1.Lease {
	f.t.Helper()
	lease, err := f.leases().Get(f.t.Context(), f.leaseName(), metav1.GetOptions{})
	if err != nil {
		f.t.Fatal(err)
	}
	return lease
}

func (f recordsFixture) updateLease(lease *coordinationv1.Lease) {
	f.t.Helper()
	if _, err := f.leases().Update(f.t.Context(), lease, metav1.UpdateOptions{}); err != nil {
		f.t.Fatal(err)
	}
}

func (f recordsFixture) readRoute() *discoveryv1.EndpointSlice {
	f.t.Helper()
	route, err := f.endpointSlices().Get(f.t.Context(), f.routeName(), metav1.GetOptions{})
	if err != nil {
		f.t.Fatal(err)
	}
	return route
}

// versions are the resource versions of the namespace's Leases and EndpointSlices, by kind and name.
func (f recordsFixture) versions() map[string]string {
	f.t.Helper()
	versions := map[string]string{}
	leases, err := f.leases().List(f.t.Context(), metav1.ListOptions{})
	if err != nil {
		f.t.Fatal(err)
	}
	for _, lease := range leases.Items {
		versions["Lease/"+lease.Name] = lease.ResourceVersion
	}
	slices, err := f.endpointSlices().List(f.t.Context(), metav1.ListOptions{})
	if err != nil {
		f.t.Fatal(err)
	}
	for _, slice := range slices.Items {
		versions["EndpointSlice/"+slice.Name] = slice.ResourceVersion
	}
	return versions
}

func TestConnectionRecordsRejectForeignObjects(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*metav1.ObjectMeta)
	}{
		{"unowned", func(m *metav1.ObjectMeta) { m.OwnerReferences = []metav1.OwnerReference{} }},
		{"previous Sandbox", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].UID = "previous-sandbox-uid" }},
		{"different API", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].APIVersion = "other.example/v1" }},
		{"different kind", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].Kind = "Other" }},
		{"different name", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].Name = "other-sandbox" }},
		{"multiple owners", func(m *metav1.ObjectMeta) {
			m.OwnerReferences = append(m.OwnerReferences, sandboxOwnerReference(&sandboxreader.Identity{Name: "other-sandbox", Uid: "other-sandbox-uid"}))
		}},
		{"deleting", func(m *metav1.ObjectMeta) { m.Finalizers = []string{testFinalizer} }},
	}
	for _, kind := range []string{"Lease", "EndpointSlice"} {
		for _, test := range cases {
			t.Run(kind+"/"+test.name, func(t *testing.T) {
				ctx := t.Context()
				f := newRecordsFixture(t)
				metadata := metav1.ObjectMeta{OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(f.identity)}}
				test.mutate(&metadata)
				var leaseUID types.UID
				if kind == "Lease" {
					lease := f.createLease(&coordinationv1.Lease{ObjectMeta: metadata})
					if test.name == "deleting" {
						if err := f.leases().Delete(ctx, lease.Name, metav1.DeleteOptions{}); err != nil {
							t.Fatal(err)
						}
					}
				} else {
					leaseUID = f.createLease(&coordinationv1.Lease{
						Spec: coordinationv1.LeaseSpec{HolderIdentity: stringPointer("gateway"), LeaseTransitions: int32Pointer(1)},
					}).UID
					route := f.route("192.0.2.1", nil)
					route.OwnerReferences, route.Finalizers = metadata.OwnerReferences, metadata.Finalizers
					f.createRoute(route)
					if test.name == "deleting" {
						if err := f.endpointSlices().Delete(ctx, route.Name, metav1.DeleteOptions{}); err != nil {
							t.Fatal(err)
						}
					}
				}
				before := f.versions()
				records := f.records(connectionHolder{name: "gateway", ip: "10.0.0.1", port: sandboxapi.ControlServicePort})
				var err error
				if kind == "Lease" {
					_, _, err = records.claim(ctx, f.identity, testPodUID)
				} else {
					err = records.pointControlEndpoints(ctx, f.identity, leaseUID, 1)
				}
				if err == nil || !strings.Contains(err.Error(), "refusing") {
					t.Fatalf("expected ownership refusal, got %v", err)
				}
				if after := f.versions(); !maps.Equal(before, after) {
					t.Fatalf("refusal changed Kubernetes state: %v, then %v", before, after)
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
			ctx := t.Context()
			f := newRecordsFixture(t)
			start := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
			writerClock := start.Add(skew)
			f.createLease(&coordinationv1.Lease{Spec: coordinationv1.LeaseSpec{
				HolderIdentity: stringPointer("test-other-gateway"), LeaseDurationSeconds: int32Pointer(leaseDurationSeconds),
				LeaseTransitions: int32Pointer(1), RenewTime: &metav1.MicroTime{Time: writerClock},
			}})
			records := f.records(connectionHolder{name: "test-gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
			now := start
			records.now = func() time.Time { return now }
			duration := time.Duration(leaseDurationSeconds) * time.Second
			refused := func(at time.Duration) {
				t.Helper()
				now = start.Add(at)
				if _, _, err := records.claim(ctx, f.identity, testPodUID); !errors.Is(err, errRunnerOwned) {
					t.Fatalf("claim at %v took a live connection: %v", at, err)
				}
			}
			refused(0)
			refused(duration - time.Second)
			// The holder renews by its own clock, which the claim never compares with this one.
			renewedAt := duration - time.Second
			lease := f.readLease()
			lease.Spec.RenewTime = &metav1.MicroTime{Time: writerClock.Add(renewedAt)}
			if _, err := f.leases().Update(ctx, lease, metav1.UpdateOptions{}); err != nil {
				t.Fatal(err)
			}
			// Expiry counts from when this replica first sees the renewal, not when it was written.
			seenAt := renewedAt + time.Second
			refused(seenAt)
			refused(seenAt + duration - time.Second)
			now = start.Add(seenAt + duration)
			epoch, _, err := records.claim(ctx, f.identity, testPodUID)
			if err != nil {
				t.Fatalf("lapsed connection was not taken over: %v", err)
			}
			if epoch != 2 {
				t.Fatalf("takeover epoch = %d, want 2", epoch)
			}
		})
	}
}

func TestConnectionReleaseAllowsImmediateTakeover(t *testing.T) {
	ctx := t.Context()
	f := newRecordsFixture(t)
	old := f.records(connectionHolder{name: "old-gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
	next := f.records(connectionHolder{name: "new-gateway", ip: "192.0.2.11", port: sandboxapi.ControlServicePort})
	epoch, leaseUID, err := old.claim(ctx, f.identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	if err := old.recordBootID(ctx, f.identity, epoch, "old-boot"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := next.claim(ctx, f.identity, testPodUID); !errors.Is(err, errRunnerOwned) {
		t.Fatalf("live claim was not protected: %v", err)
	}
	if err := old.release(ctx, f.identity, leaseUID, epoch); err != nil {
		t.Fatal(err)
	}
	lease := f.readLease()
	if lease.Spec.HolderIdentity != nil || lease.Spec.RenewTime != nil || *lease.Spec.LeaseTransitions != int32(epoch) ||
		lease.Annotations[sandboxlease.EnvironmentRuntimeBootIDAnnotation] != "" || sandboxlease.LastConnectedPodUID(lease.Annotations) != testPodUID {
		t.Fatalf("release erased history or retained a live claim: %+v", lease)
	}
	newEpoch, _, err := next.claim(ctx, f.identity, testPodUID)
	if err != nil || newEpoch != epoch+1 {
		t.Fatalf("released Lease did not allow immediate takeover: epoch=%d, err=%v", newEpoch, err)
	}
	route := f.readRoute()
	if len(route.Endpoints) != 1 || len(route.Endpoints[0].Addresses) != 1 || route.Endpoints[0].Addresses[0] != "192.0.2.11" {
		t.Fatalf("takeover did not repoint the control route: %+v", route.Endpoints)
	}
}

func TestConnectionReleaseCannotClearSupersedingClaim(t *testing.T) {
	for _, test := range []struct {
		name string
		// supersede replaces the old Gateway's claim on lease, as a successor does.
		supersede func(f recordsFixture, lease *coordinationv1.Lease)
	}{
		{"newer epoch on same holder", func(f recordsFixture, lease *coordinationv1.Lease) {
			*lease.Spec.LeaseTransitions++
			f.updateLease(lease)
		}},
		{"new holder", func(f recordsFixture, lease *coordinationv1.Lease) {
			lease.Spec.HolderIdentity = stringPointer("new-gateway")
			f.updateLease(lease)
		}},
		{"recreated Lease UID", func(f recordsFixture, lease *coordinationv1.Lease) {
			if err := f.leases().Delete(f.t.Context(), lease.Name, metav1.DeleteOptions{}); err != nil {
				f.t.Fatal(err)
			}
			f.createLease(&coordinationv1.Lease{
				ObjectMeta: metav1.ObjectMeta{Annotations: lease.Annotations, Labels: lease.Labels},
				Spec:       lease.Spec,
			})
		}},
		{"recreated Sandbox UID", func(f recordsFixture, lease *coordinationv1.Lease) {
			lease.OwnerReferences[0].UID = "replacement-sandbox"
			f.updateLease(lease)
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			ctx := t.Context()
			f := newRecordsFixture(t)
			old := f.records(connectionHolder{name: "old-gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
			epoch, leaseUID, err := old.claim(ctx, f.identity, testPodUID)
			if err != nil {
				t.Fatal(err)
			}
			test.supersede(f, f.readLease())
			if err := old.release(ctx, f.identity, leaseUID, epoch); !errors.Is(err, errSupersededConnection) {
				t.Fatalf("stale release result = %v, want superseded", err)
			}
			if current := f.readLease(); current.Spec.HolderIdentity == nil || current.Spec.RenewTime == nil {
				t.Fatalf("stale release cleared successor: %+v", current.Spec)
			}
		})
	}
}

// racingCoordination runs beforeUpdate just before each Lease update reaches the API server, so a test can change
// the Lease under an update already in flight.
type racingCoordination struct {
	coordinationclient.CoordinationV1Interface
	beforeUpdate func(*coordinationv1.Lease)
}

func (c racingCoordination) Leases(namespace string) coordinationclient.LeaseInterface {
	return racingLeases{LeaseInterface: c.CoordinationV1Interface.Leases(namespace), beforeUpdate: c.beforeUpdate}
}

type racingLeases struct {
	coordinationclient.LeaseInterface
	beforeUpdate func(*coordinationv1.Lease)
}

func (l racingLeases) Update(ctx context.Context, lease *coordinationv1.Lease, options metav1.UpdateOptions) (*coordinationv1.Lease, error) {
	l.beforeUpdate(lease)
	return l.LeaseInterface.Update(ctx, lease, options)
}

func TestConnectionReleaseRechecksClaimAfterUpdateConflict(t *testing.T) {
	ctx := t.Context()
	f := newRecordsFixture(t)
	holder := connectionHolder{name: "old-gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort}
	epoch, leaseUID, err := f.records(holder).claim(ctx, f.identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	// A successor claims the Lease after the release read it and before its update lands, so the update conflicts.
	intercepted := false
	old := f.recordsWith(racingCoordination{
		CoordinationV1Interface: api.Clientset.CoordinationV1(),
		beforeUpdate: func(*coordinationv1.Lease) {
			if intercepted {
				return
			}
			intercepted = true
			successor := f.readLease()
			successor.Spec.HolderIdentity = stringPointer("new-gateway")
			*successor.Spec.LeaseTransitions++
			f.updateLease(successor)
		},
	}, api.Clientset.DiscoveryV1(), holder)
	if err := old.release(ctx, f.identity, leaseUID, epoch); !errors.Is(err, errSupersededConnection) || !intercepted {
		t.Fatalf("racing release = %v, intercepted=%t", err, intercepted)
	}
	current := f.readLease()
	if current.Spec.HolderIdentity == nil || *current.Spec.HolderIdentity != "new-gateway" ||
		*current.Spec.LeaseTransitions != int32(epoch+1) || current.Spec.RenewTime == nil {
		t.Fatalf("stale release cleared the racing successor: %+v", current.Spec)
	}
}

func TestConnectionRenewPublishesAndClearsAutosuspendBlockers(t *testing.T) {
	ctx := t.Context()
	f := newRecordsFixture(t)
	records := f.records(connectionHolder{name: "test-gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
	epoch, _, err := records.claim(ctx, f.identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	sessionID := "session-a"
	busy := &sandboxlease.EnvironmentRuntimeFacts{AutosuspendBlockers: []sandboxapi.AutosuspendBlocker{{
		Reason: sandboxapi.AutosuspendBlockerActiveTurn, SessionID: &sessionID,
	}}}
	if err := records.renew(ctx, f.identity, epoch, time.Now(), busy); err != nil {
		t.Fatal(err)
	}
	if f.readLease().Annotations[sandboxlease.AutosuspendBlockersAnnotation] == "" {
		t.Fatal("busy runner facts were not published on the connection Lease")
	}
	if err := records.renew(ctx, f.identity, epoch, time.Now(), &sandboxlease.EnvironmentRuntimeFacts{}); err != nil {
		t.Fatal(err)
	}
	if blockers, exists := f.readLease().Annotations[sandboxlease.AutosuspendBlockersAnnotation]; exists {
		t.Fatalf("idle runner retained blocker annotation: %s", blockers)
	}
}

func TestObservationClaimsPreserveVerifiedContact(t *testing.T) {
	ctx := t.Context()
	f := newRecordsFixture(t)
	records := f.records(connectionHolder{name: "gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
	epoch, _, err := records.claim(ctx, f.identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	if sandboxlease.LastConnectedPodUID(f.readLease().Annotations) != "" {
		t.Fatal("claim counted as contact")
	}
	if err := records.recordBootID(ctx, f.identity, epoch, "boot"); err != nil {
		t.Fatal(err)
	}
	for _, uid := range []string{testPodUID, "replacement-pod"} {
		epoch, _, err = records.claim(ctx, f.identity, uid)
		if err != nil {
			t.Fatal(err)
		}
		lease := f.readLease()
		if lease.Annotations[sandboxlease.EnvironmentRuntimeBootIDAnnotation] != "" {
			t.Fatal("claim retained current boot ID")
		}
		if sandboxlease.LastConnectedPodUID(lease.Annotations) != testPodUID {
			t.Fatal("claim erased verified contact")
		}
	}
	if err := records.recordBootID(ctx, f.identity, epoch, "replacement-boot"); err != nil {
		t.Fatal(err)
	}
	if sandboxlease.LastConnectedPodUID(f.readLease().Annotations) != "replacement-pod" {
		t.Fatal("verified replacement did not update contact")
	}
}

func TestControlRouteRecoversAfterLeaseRecreation(t *testing.T) {
	ctx := t.Context()
	f := newRecordsFixture(t)
	oldLease := f.createLease(&coordinationv1.Lease{Spec: coordinationv1.LeaseSpec{
		HolderIdentity: stringPointer("test-old-gateway"), LeaseTransitions: int32Pointer(9),
	}})
	f.createRoute(f.route("192.0.2.40", map[string]string{
		sandboxlease.ConnectionEpochAnnotation:    "9",
		sandboxlease.ConnectionLeaseUIDAnnotation: string(oldLease.UID),
	}))
	if err := f.leases().Delete(ctx, oldLease.Name, metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	current := f.records(connectionHolder{name: "test-new-gateway", ip: "192.0.2.41", port: sandboxapi.ControlServicePort})
	epoch, _, err := current.claim(ctx, f.identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	if epoch != 10 {
		t.Fatalf("recreated Lease epoch = %d, want 10 above the retained route", epoch)
	}
	lease := f.readLease()
	if lease.UID == oldLease.UID || *lease.Spec.LeaseTransitions != 10 {
		t.Fatalf("recreated Lease = UID %q, transitions %d", lease.UID, *lease.Spec.LeaseTransitions)
	}
	if err := current.pointControlEndpoints(ctx, f.identity, oldLease.UID, 9); !errors.Is(err, errSupersededConnection) {
		t.Fatalf("old Lease publication error = %v, want superseded", err)
	}
	updated := f.readRoute()
	if updated.Annotations[sandboxlease.ConnectionEpochAnnotation] != "10" ||
		updated.Annotations[sandboxlease.ConnectionLeaseUIDAnnotation] != string(lease.UID) ||
		len(updated.Endpoints) != 1 || updated.Endpoints[0].Addresses[0] != "192.0.2.41" {
		t.Fatalf("control route after Lease recreation = %+v", updated)
	}
}

func TestControlRouteEpochUsesLegacyBaselineAndRejectsInvalidValues(t *testing.T) {
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
			f := newRecordsFixture(t)
			var annotations map[string]string
			if test.annotation != "" {
				annotations = map[string]string{sandboxlease.ConnectionEpochAnnotation: test.annotation}
			}
			f.createRoute(f.route("192.0.2.60", annotations))
			records := f.records(connectionHolder{name: "gateway", ip: "192.0.2.60", port: sandboxapi.ControlServicePort})
			epoch, err := records.controlRouteEpoch(t.Context(), f.identity)
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

// racingDiscovery runs beforeUpdate just before each control-route update reaches the API server, so a test can
// change the route under a publication already in flight.
type racingDiscovery struct {
	discoveryclient.DiscoveryV1Interface
	beforeUpdate func(*discoveryv1.EndpointSlice)
}

func (d racingDiscovery) EndpointSlices(namespace string) discoveryclient.EndpointSliceInterface {
	return racingEndpointSlices{EndpointSliceInterface: d.DiscoveryV1Interface.EndpointSlices(namespace), beforeUpdate: d.beforeUpdate}
}

type racingEndpointSlices struct {
	discoveryclient.EndpointSliceInterface
	beforeUpdate func(*discoveryv1.EndpointSlice)
}

func (s racingEndpointSlices) Update(ctx context.Context, slice *discoveryv1.EndpointSlice, options metav1.UpdateOptions) (*discoveryv1.EndpointSlice, error) {
	s.beforeUpdate(slice)
	return s.EndpointSliceInterface.Update(ctx, slice, options)
}

func TestDelayedRoutePublicationCannotRegressRecreatedLeaseRoute(t *testing.T) {
	ctx := t.Context()
	f := newRecordsFixture(t)
	oldLease := f.createLease(&coordinationv1.Lease{Spec: coordinationv1.LeaseSpec{
		HolderIdentity: stringPointer("test-old-gateway"), LeaseTransitions: int32Pointer(2),
	}})
	f.createRoute(f.route("192.0.2.50", map[string]string{
		sandboxlease.ConnectionEpochAnnotation:    "1",
		sandboxlease.ConnectionLeaseUIDAnnotation: string(oldLease.UID),
	}))
	// While the old Gateway's publication is in flight, a new Gateway recreates the Lease and publishes its route.
	var newLease *coordinationv1.Lease
	transitioned := false
	oldRecords := f.recordsWith(api.Clientset.CoordinationV1(), racingDiscovery{
		DiscoveryV1Interface: api.Clientset.DiscoveryV1(),
		beforeUpdate: func(slice *discoveryv1.EndpointSlice) {
			if transitioned || slice.Endpoints[0].Addresses[0] != "192.0.2.50" {
				return
			}
			transitioned = true
			if err := f.leases().Delete(ctx, oldLease.Name, metav1.DeleteOptions{}); err != nil {
				t.Fatal(err)
			}
			newLease = f.createLease(&coordinationv1.Lease{Spec: coordinationv1.LeaseSpec{
				HolderIdentity: stringPointer("test-new-gateway"), LeaseTransitions: int32Pointer(2),
			}})
			route := f.readRoute()
			route.Annotations = map[string]string{
				sandboxlease.ConnectionEpochAnnotation:    "2",
				sandboxlease.ConnectionLeaseUIDAnnotation: string(newLease.UID),
			}
			route.Endpoints[0].Addresses[0] = "192.0.2.51"
			if _, err := f.endpointSlices().Update(ctx, route, metav1.UpdateOptions{}); err != nil {
				t.Fatal(err)
			}
		},
	}, connectionHolder{name: "test-old-gateway", ip: "192.0.2.50", port: sandboxapi.ControlServicePort})
	if err := oldRecords.pointControlEndpoints(ctx, f.identity, oldLease.UID, 2); !errors.Is(err, errSupersededConnection) {
		t.Fatalf("delayed old publication error = %v, want superseded", err)
	}
	if !transitioned {
		t.Fatal("the old publication never reached its update")
	}
	updated := f.readRoute()
	if updated.Annotations[sandboxlease.ConnectionLeaseUIDAnnotation] != string(newLease.UID) ||
		updated.Annotations[sandboxlease.ConnectionEpochAnnotation] != "2" ||
		updated.Endpoints[0].Addresses[0] != "192.0.2.51" {
		t.Fatalf("delayed old publication regressed control route: %+v", updated)
	}
}
