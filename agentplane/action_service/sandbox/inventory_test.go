package sandbox

import (
	"encoding/json"
	"errors"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/requester"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

// A delegated Identity box runs as the namespace's default ServiceAccount; its record names the Identity and is
// unowned, so the box's lifecycle is explicit and deleting the Identity only revokes its credential.
func TestADelegatedIdentityBoxRunsAsDefaultAndItsRecordIsUnowned(t *testing.T) {
	f := newFixture(t, nil)
	if _, err := f.create(t, f.inventory(), f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	pod := f.pod(t, f.caller, "box")
	record := f.record(t, f.caller, "box")
	if pod.Spec.ServiceAccountName != defaultServiceAccount {
		t.Errorf("the box runs as %q, want %q", pod.Spec.ServiceAccountName, defaultServiceAccount)
	}
	if owner := metav1.GetControllerOf(pod); owner == nil || owner.Kind != workloadapi.Kind || owner.UID != record.UID || len(pod.OwnerReferences) != 1 {
		t.Errorf("the box is owned by %v, want its WorkloadCredential %s", pod.OwnerReferences, record.UID)
	}
	if len(record.OwnerReferences) != 0 {
		t.Errorf("the box's credential is owned by %+v, want no owner", record.OwnerReferences)
	}
	if subject := record.Spec.Subject; subject.ServiceAccount != "" || subject.Identity == nil ||
		*subject.Identity != (workloadapi.IdentitySubject{Name: f.caller.Name, UID: types.UID(f.caller.UID)}) {
		t.Errorf("the box's credential names %+v, want its delegated Identity", subject)
	}
	if pod.Labels["app.kubernetes.io/name"] != "test-box" {
		t.Errorf("the box's labels %v dropped the template's", pod.Labels)
	}
}

// An Identity's box runs on the group namespace's default ServiceAccount, in a group outside the policy namespace that
// holds the Identity. Its SVID resolves to the Identity, with the box as its Pod, as the egress proxy and the services
// resolve a Sandbox's: the box acts as the Identity, by the Identity's bindings. Nothing owns its record, so the box
// outlives the Identity until it is disposed of: deleting the Identity revokes the SVID and deletes nothing. Neither a
// same-named successor Identity nor a same-named account reaches the box, or creates one of its name while it stands.
func TestAnIdentitysBoxOutlivesTheIdentityWithItsCredentialRevoked(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, nil)
	policyNamespace := f.identityNamespace
	var live policyapi.Identity
	if err := objects.Get(ctx, types.NamespacedName{Namespace: policyNamespace, Name: f.caller.Name}, &live); err != nil {
		t.Fatal(err)
	}
	agent := f.caller
	inventory := f.inventory()
	if _, err := f.create(t, inventory, agent, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	f.issuance.Settle(t)
	pod := f.pod(t, agent, "box")
	record := f.record(t, agent, "box")
	if pod.Spec.ServiceAccountName != "default" {
		t.Errorf("the box runs as %q, want the default ServiceAccount", pod.Spec.ServiceAccountName)
	}
	if owner := metav1.GetControllerOf(pod); owner == nil || owner.Kind != workloadapi.Kind || owner.UID != record.UID {
		t.Errorf("the box is owned by %v, want its WorkloadCredential %s", pod.OwnerReferences, record.UID)
	}
	// What the garbage collector, which this test API server does not run, would collect the record by.
	if len(record.OwnerReferences) != 0 {
		t.Errorf("the box's record is owned by %+v, want no owner", record.OwnerReferences)
	}
	if subject := record.Spec.Subject; subject.ServiceAccount != "" || subject.Identity == nil ||
		*subject.Identity != (workloadapi.IdentitySubject{Name: agent.Name, UID: types.UID(agent.UID)}) {
		t.Errorf("the box's record names %+v, want the Identity", subject)
	}
	resolves, err := resolver.New(objects, []string{f.binding.Namespace}, &resolver.Identities{PolicyNamespace: policyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	leaf := f.leaf(t, record)
	peer, err := resolves.Resolve(ctx, leaf)
	if err != nil || peer.Subject != (resolver.Identity{Name: agent.Name, UID: live.UID}) || peer.Pod == nil || peer.Pod.UID != pod.UID {
		t.Errorf("the box's SVID resolves to %+v, %v; want the Identity and Pod %s", peer, err, pod.UID)
	}
	if boxes, err := inventory.List(ctx, agent); err != nil || len(boxes) != 1 {
		t.Errorf("the Identity lists %v, %v", boxes, err)
	}

	if err := objects.Delete(ctx, live); err != nil {
		t.Fatal(err)
	}
	successor := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: live.Name}, Spec: live.Spec}
	if err := objects.Create(ctx, successor); err != nil {
		t.Fatal(err)
	}
	if peer, err := resolves.Resolve(ctx, leaf); !errors.Is(err, resolver.ErrRefused) {
		t.Errorf("the deleted Identity's box SVID resolves to %+v, %v; want it refused", peer, err)
	}
	for name, other := range map[string]Identity{"the successor Identity": {Name: successor.Name, UID: string(successor.UID)}} {
		var foreign *ForeignError
		if _, err := inventory.Get(ctx, other, "box"); !errors.As(err, &foreign) {
			t.Errorf("%s: Get() = %v, want a ForeignError", name, err)
		}
		if boxes, err := inventory.List(ctx, other); err != nil || len(boxes) != 0 {
			t.Errorf("%s lists %v, %v", name, boxes, err)
		}
		if _, err := f.create(t, inventory, other, "box", testTemplate); !errors.As(err, &foreign) {
			t.Errorf("%s: Create() = %v, want a ForeignError", name, err)
		}
		if existed, err := inventory.Dispose(ctx, other, "box"); !errors.As(err, &foreign) || existed {
			t.Errorf("%s: Dispose() = %t, %v; want a ForeignError", name, existed, err)
		}
	}
	if kept := f.pod(t, agent, "box"); kept.UID != pod.UID {
		t.Errorf("the box is now Pod %s, want %s", kept.UID, pod.UID)
	}
	if kept := f.record(t, agent, "box"); kept.UID != record.UID || kept.Spec.Subject.Identity.UID != live.UID {
		t.Errorf("the box's record is now %s for %+v, want %s for the deleted Identity", kept.UID, kept.Spec.Subject, record.UID)
	}

	if existed, err := inventory.Dispose(ctx, agent, "box"); err != nil || !existed {
		t.Fatalf("Dispose() = %t, %v", existed, err)
	}
	if err := objects.Get(ctx, types.NamespacedName{Namespace: f.binding.Namespace, Name: record.Name}, &workloadapi.WorkloadCredential{}); !apierrors.IsNotFound(err) {
		t.Errorf("the disposed box's record survived: %v", err)
	}
}

// The box's record names its caller and binds its Pod, whose sidecar alone mounts the credential issued for it.
func TestABoxPresentsACredentialOfItsOwnFromItsSidecarAlone(t *testing.T) {
	f := newFixture(t, nil)
	if _, err := f.create(t, f.inventory(), f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	f.issuance.Settle(t)
	pod := f.pod(t, f.caller, "box")
	record := f.record(t, f.caller, "box")
	bound := workloadapi.Binding{PodUID: pod.UID}
	if record.Spec.Subject.ServiceAccount != "" || record.Spec.Subject.Identity == nil ||
		*record.Spec.Subject.Identity != (workloadapi.IdentitySubject{Name: f.caller.Name, UID: types.UID(f.caller.UID)}) ||
		record.Spec.Binding == nil || *record.Spec.Binding != bound ||
		record.Spec.Pod == nil || *record.Spec.Pod != (workloadapi.PodRef{Name: pod.Name, UID: pod.UID}) {
		t.Errorf("the record asks for %+v, want the caller bound to box %s", record.Spec, pod.UID)
	}
	if accepted := record.Status.AcceptedBinding; accepted == nil || *accepted != bound || pod.Labels[credentialLabel] != record.Status.CredentialID {
		t.Errorf("the record accepted %v for %s; the box was started with %s", accepted, record.Status.CredentialID, pod.Labels[credentialLabel])
	}
	volume := one(t, pod.Spec.Volumes, func(volume corev1.Volume) bool { return volume.Name == svidVolume })
	if volume.Secret == nil || volume.Secret.SecretName != record.Status.SecretName {
		t.Errorf("the SVID volume is %+v, want the record's Secret %s", volume.VolumeSource, record.Status.SecretName)
	}
	for _, container := range pod.Spec.Containers {
		mounts := slices.ContainsFunc(container.VolumeMounts, func(mount corev1.VolumeMount) bool { return mount.Name == svidVolume })
		if mounts != (container.Name == egressSidecar) {
			t.Errorf("container %s mounts the SVID: %t", container.Name, mounts)
		}
	}
}

// A box made from a template with no egress sidecar has nothing that could present a credential, and mounts none.
func TestAnOfflineBoxMountsNoCredential(t *testing.T) {
	f := newFixture(t, func(spec *corev1.PodSpec) { spec.Containers = spec.Containers[:1] })
	if _, err := f.create(t, f.inventory(), f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	if pod := f.pod(t, f.caller, "box"); slices.ContainsFunc(pod.Spec.Volumes, func(volume corev1.Volume) bool { return volume.Name == svidVolume }) {
		t.Errorf("the offline box has the SVID volume: %+v", pod.Spec.Volumes)
	}
}

// Disposing revokes the box's credential, and the next box of the name gets another: the verifier accepts the
// box's SVID and refuses the earlier box's.
func TestARecreatedBoxsCredentialReplacesTheDisposedOnes(t *testing.T) {
	f := newFixture(t, nil)
	resolves, err := resolver.New(objects, []string{f.binding.Namespace}, &resolver.Identities{PolicyNamespace: f.identityNamespace})
	if err != nil {
		t.Fatal(err)
	}
	inventory := f.inventory()
	if _, err := f.create(t, inventory, f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	f.issuance.Settle(t)
	first := f.pod(t, f.caller, "box")
	firstLeaf := f.leaf(t, f.record(t, f.caller, "box"))
	peer, err := resolves.Resolve(t.Context(), firstLeaf)
	if err != nil {
		t.Fatal(err)
	}
	if subject, ok := peer.Subject.(resolver.Identity); !ok || subject.Name != f.caller.Name || subject.UID != types.UID(f.caller.UID) || peer.Pod == nil || peer.Pod.UID != first.UID {
		t.Errorf("the box's SVID resolves to %+v, want its caller and Pod %s", peer, first.UID)
	}

	if existed, err := inventory.Dispose(t.Context(), f.caller, "box"); err != nil || !existed {
		t.Fatalf("Dispose() = %t, %v", existed, err)
	}
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: f.binding.Namespace, Name: first.Name}, &workloadapi.WorkloadCredential{}); !apierrors.IsNotFound(err) {
		t.Errorf("the disposed box's record survived: %v", err)
	}
	if _, err := resolves.Resolve(t.Context(), firstLeaf); !errors.Is(err, resolver.ErrRefused) {
		t.Errorf("the disposed box's SVID resolves: %v", err)
	}

	if _, err := f.create(t, inventory, f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	f.issuance.Settle(t)
	second := f.pod(t, f.caller, "box")
	record := f.record(t, f.caller, "box")
	if second.Labels[credentialLabel] == first.Labels[credentialLabel] || record.Status.CredentialID != second.Labels[credentialLabel] {
		t.Errorf("the recreated box runs with credential %s after %s; its record issues %s",
			second.Labels[credentialLabel], first.Labels[credentialLabel], record.Status.CredentialID)
	}
	if _, err := resolves.Resolve(t.Context(), firstLeaf); !errors.Is(err, resolver.ErrRefused) {
		t.Errorf("the earlier box's SVID resolves against its successor's record: %v", err)
	}
	if peer, err := resolves.Resolve(t.Context(), f.leaf(t, record)); err != nil || peer.Pod == nil || peer.Pod.UID != second.UID {
		t.Errorf("the recreated box's SVID resolves to %+v, %v; want Pod %s", peer, err, second.UID)
	}
}

// A record that has moved to a credential the box does not hold is left unbound, rather than accepting for the box an
// ID it never had.
func TestABoxIsNotBoundToACredentialItWasNotStartedWith(t *testing.T) {
	f := newFixture(t, nil)
	inventory := f.inventory()
	if _, err := f.create(t, inventory, f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	f.issuance.Settle(t)
	credentials, err := requester.New(objects, objects)
	if err != nil {
		t.Fatal(err)
	}
	// Unbinding the record, as a create of the name that found no box would, rotates its credential.
	if _, err := credentials.Ensure(t.Context(), credentialOwner(f.binding.Namespace, f.caller, "box")); err != nil {
		t.Fatal(err)
	}
	f.issuance.Settle(t)
	if _, err := f.create(t, inventory, f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	if record := f.record(t, f.caller, "box"); record.Spec.Binding != nil || record.Status.CredentialID == f.pod(t, f.caller, "box").Labels[credentialLabel] {
		t.Errorf("the record binds %+v issuing %s; want it unbound, issuing a credential the box was not started with",
			record.Spec.Binding, record.Status.CredentialID)
	}
}

func TestTemplatesCannotProjectServiceAccountTokens(t *testing.T) {
	tokenMountedIn := func(container int) func(*corev1.PodSpec) {
		return func(spec *corev1.PodSpec) {
			spec.Volumes = []corev1.Volume{{Name: "test-token", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
				Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{Path: "token"}}},
			}}}}
			spec.Containers[container].VolumeMounts = []corev1.VolumeMount{{Name: "test-token", MountPath: "/var/run/test"}}
		}
	}
	for _, test := range []struct {
		name    string
		edit    func(*corev1.PodSpec)
		refused bool
	}{
		{"offline without the egress sidecar", func(spec *corev1.PodSpec) { spec.Containers = spec.Containers[:1] }, false},
		{"offline workload mounting a token", func(spec *corev1.PodSpec) {
			spec.Containers = spec.Containers[:1]
			tokenMountedIn(0)(spec)
		}, true},
		{"automounting a token", func(spec *corev1.PodSpec) { spec.AutomountServiceAccountToken = nil }, true},
		{"mounting a token in the workload", tokenMountedIn(0), true},
		{"mounting a token in the egress sidecar", tokenMountedIn(1), true},
		{"defining the SVID volume", func(spec *corev1.PodSpec) {
			spec.Volumes = []corev1.Volume{{Name: svidVolume, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}}
		}, true},
		{"mounting at the SVID's path", func(spec *corev1.PodSpec) {
			spec.Volumes = []corev1.Volume{{Name: "test-other", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}}
			spec.Containers[0].VolumeMounts = []corev1.VolumeMount{{Name: "test-other", MountPath: svidMountPath}}
		}, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			err := newFixture(t, test.edit).inventory().CheckTemplates(t.Context())
			var refusal *TemplateError
			if errors.As(err, &refusal) != test.refused || (!test.refused && err != nil) {
				t.Errorf("CheckTemplates() = %v; refusal wanted: %t", err, test.refused)
			}
		})
	}
}

func TestAMissingTemplateFailsTheCheck(t *testing.T) {
	f := newFixture(t, nil)
	f.binding.Templates["missing-template"] = "a box with no template"
	if err := f.inventory().CheckTemplates(t.Context()); !apierrors.IsNotFound(err) {
		t.Errorf("CheckTemplates() = %v, want the API server's NotFound", err)
	}
}

func TestATemplateThisDeploymentDoesNotOfferIsRefused(t *testing.T) {
	f := newFixture(t, nil)
	var unavailable *UnavailableError
	if _, err := f.create(t, f.inventory(), f.caller, "box", "unoffered"); !errors.As(err, &unavailable) {
		t.Errorf("Create() = %v, want an UnavailableError", err)
	}
	if _, err := f.inventory().Template(t.Context(), "unoffered"); !errors.As(err, &unavailable) {
		t.Errorf("Template() = %v, want an UnavailableError", err)
	}
}

func TestCreatingAnExistingBoxReturnsItWithoutStampingAnother(t *testing.T) {
	f := newFixture(t, nil)
	if _, err := f.create(t, f.inventory(), f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	first := f.pod(t, f.caller, "box")
	f.report(t, f.caller, "box", ready)
	again, err := f.create(t, f.inventory(), f.caller, "box", testTemplate)
	if err != nil {
		t.Fatal(err)
	}
	if second := f.pod(t, f.caller, "box"); second.UID != first.UID {
		t.Errorf("the box was stamped again: %s replaced %s", second.UID, first.UID)
	}
	if again.Phase == nil || *again.Phase != string(corev1.PodRunning) {
		t.Errorf("Create() reports phase %v, want the existing box's Running", again.Phase)
	}
}

// A full namespace quota refuses the Pod outright, and the caller can act on that by disposing of boxes it has
// finished with.
func TestARefusedCreateSaysWhatTheAPIServerSaid(t *testing.T) {
	f := newFixture(t, nil)
	quotas := server.Clientset.CoreV1().ResourceQuotas(f.binding.Namespace)
	none := corev1.ResourceList{corev1.ResourcePods: resource.MustParse("0")}
	quota, err := quotas.Create(t.Context(), &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Name: "test-quota"}, Spec: corev1.ResourceQuotaSpec{Hard: none},
	}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	// What the quota controller, which does not run here, would write.
	quota.Status = corev1.ResourceQuotaStatus{Hard: none, Used: none}
	if _, err := quotas.UpdateStatus(t.Context(), quota, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	_, err = f.create(t, f.inventory(), f.caller, "box", testTemplate)
	var unavailable *UnavailableError
	if !errors.As(err, &unavailable) || !strings.Contains(unavailable.Message, "exceeded quota: test-quota") {
		t.Errorf("Create() = %v, want an UnavailableError carrying the API server's refusal", err)
	}
}

func TestAnotherCallersBoxOfThatNameIsNotReached(t *testing.T) {
	for _, test := range []struct {
		name   string
		labels func(other Identity) map[string]string
	}{
		{"another caller's box", func(other Identity) map[string]string { return boxLabels(other, "box", testTemplate) }},
		{"a Pod no Action made", func(Identity) map[string]string { return map[string]string{nameLabel: "box"} }},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newFixture(t, nil)
			other := workloadIdentity(t, f.identityNamespace, "test-other")
			_, err := server.Clientset.CoreV1().Pods(f.binding.Namespace).Create(t.Context(), &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{Name: objectName(f.caller, "box"), Labels: test.labels(other)},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "workspace", Image: "registry.test/unused"}}},
			}, metav1.CreateOptions{})
			if err != nil {
				t.Fatal(err)
			}
			var foreign *ForeignError
			if _, err := f.inventory().Get(t.Context(), f.caller, "box"); !errors.As(err, &foreign) {
				t.Errorf("Get() = %v, want a ForeignError", err)
			}
			if _, err := f.inventory().Dispose(t.Context(), f.caller, "box"); !errors.As(err, &foreign) {
				t.Errorf("Dispose() = %v, want a ForeignError", err)
			}
		})
	}
}

func TestAListHoldsOnlyTheCallersBoxes(t *testing.T) {
	f := newFixture(t, nil)
	other := workloadIdentity(t, f.identityNamespace, "test-other")
	for _, caller := range []struct {
		identity Identity
		box      string
	}{{f.caller, "mine"}, {other, "theirs"}} {
		if _, err := f.create(t, f.inventory(), caller.identity, caller.box, testTemplate); err != nil {
			t.Fatal(err)
		}
	}
	boxes, err := f.inventory().List(t.Context(), f.caller)
	if err != nil {
		t.Fatal(err)
	}
	if len(boxes) != 1 || boxes[0].Name != "mine" {
		t.Errorf("List() = %v, want only the caller's box", boxes)
	}
}

func TestDisposingDeletesTheBoxOnce(t *testing.T) {
	f := newFixture(t, nil)
	if _, err := f.create(t, f.inventory(), f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	for _, existed := range []bool{true, false} {
		if got, err := f.inventory().Dispose(t.Context(), f.caller, "box"); err != nil || got != existed {
			t.Errorf("Dispose() = %t, %v; want %t", got, err, existed)
		}
	}
	var unavailable *UnavailableError
	if _, err := f.inventory().Get(t.Context(), f.caller, "box"); !errors.As(err, &unavailable) {
		t.Errorf("Get() of a disposed box = %v, want an UnavailableError", err)
	}
}

// A Pod reports more than readiness, and summarising it here would drop the rest: an unschedulable box and one still
// pulling its image are both not Ready.
func TestEveryConditionReachesTheCallerAsKubernetesWroteIt(t *testing.T) {
	f := newFixture(t, nil)
	if _, err := f.create(t, f.inventory(), f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	unschedulable := metav1.NewTime(time.Date(2026, 9, 19, 9, 49, 30, 0, time.UTC))
	f.report(t, f.caller, "box", corev1.PodStatus{
		Phase: corev1.PodPending,
		Conditions: []corev1.PodCondition{
			{
				Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Reason: "Unschedulable",
				Message: "0/3 nodes are available", LastTransitionTime: unschedulable,
			},
			{Type: corev1.PodReady, Status: corev1.ConditionFalse, Reason: "ContainersNotReady"},
		},
	})
	box, err := f.inventory().Get(t.Context(), f.caller, "box")
	if err != nil {
		t.Fatal(err)
	}
	want := []Condition{
		{
			Type: "PodScheduled", Status: "False", Reason: new("Unschedulable"), Message: new("0/3 nodes are available"),
			LastTransitionTime: &unschedulable.Time,
		},
		{Type: "Ready", Status: "False", Reason: new("ContainersNotReady")},
	}
	if !reflect.DeepEqual(box.Conditions, want) {
		t.Errorf("conditions = %#v, want %#v", box.Conditions, want)
	}
}

// Kubernetes has written no condition between the create call and the read that follows it.
func TestABoxWithNoConditionsYetReportsNone(t *testing.T) {
	f := newFixture(t, nil)
	box, err := f.create(t, f.inventory(), f.caller, "box", testTemplate)
	if err != nil {
		t.Fatal(err)
	}
	document, err := json.Marshal(box)
	if err != nil {
		t.Fatal(err)
	}
	var read struct {
		Conditions []Condition `json:"conditions"`
	}
	if err := json.Unmarshal(document, &read); err != nil || read.Conditions == nil || len(read.Conditions) != 0 {
		t.Errorf("a new box reads %s, want an empty list of conditions", document)
	}
}

func TestExecRunsInTheContainerKubectlExecWouldPick(t *testing.T) {
	for _, test := range []struct {
		name        string
		annotations map[string]string
		container   string
	}{
		{"the first", nil, "workspace"},
		{"the default", map[string]string{defaultContainerAnnotation: egressSidecar}, egressSidecar},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newFixture(t, nil)
			if _, err := f.create(t, f.inventory(), f.caller, "box", testTemplate); err != nil {
				t.Fatal(err)
			}
			pod := f.pod(t, f.caller, "box")
			pod.Annotations = test.annotations
			if _, err := server.Clientset.CoreV1().Pods(f.binding.Namespace).Update(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
				t.Fatal(err)
			}
			f.report(t, f.caller, "box", ready)
			if _, err := f.inventory().Run(t.Context(), f.caller, "box", Script{Text: "true", TimeoutSeconds: 5, MaxOutputBytes: 100}); err != nil {
				t.Fatal(err)
			}
			want := []target{{types.NamespacedName{Namespace: f.binding.Namespace, Name: pod.Name}, test.container}}
			if !reflect.DeepEqual(f.exec.ran, want) {
				t.Errorf("ran in %v, want %v", f.exec.ran, want)
			}
		})
	}
}

func TestExecInABoxThatIsNotReadySaysWhy(t *testing.T) {
	f := newFixture(t, nil)
	if _, err := f.create(t, f.inventory(), f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	f.report(t, f.caller, "box", corev1.PodStatus{
		Phase: corev1.PodPending,
		Conditions: []corev1.PodCondition{{
			Type: corev1.PodReady, Status: corev1.ConditionFalse, Reason: "ContainersNotReady",
			Message: "containers with unready status: [workspace]",
		}},
	})
	_, err := f.inventory().Run(t.Context(), f.caller, "box", Script{Text: "true", TimeoutSeconds: 5, MaxOutputBytes: 100})
	var unavailable *UnavailableError
	if !errors.As(err, &unavailable) || !strings.Contains(unavailable.Message, "containers with unready status: [workspace]") {
		t.Errorf("Run() = %v, want an UnavailableError carrying the Ready condition's message", err)
	}
	if len(f.exec.ran) != 0 {
		t.Errorf("exec ran in %v", f.exec.ran)
	}
}
