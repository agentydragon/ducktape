package sandbox

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

// A Session Sandbox's agent that made boxes loses them with its account.
func TestABoxRunsAsItsCallerAndGoesWithItsAccount(t *testing.T) {
	f := newFixture(t, nil)
	if _, err := f.inventory().Create(t.Context(), f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	pod := f.pod(t, f.caller, "box")
	account, err := server.Clientset.CoreV1().ServiceAccounts(f.binding.Namespace).Get(t.Context(), f.caller.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if pod.Spec.ServiceAccountName != f.caller.Name {
		t.Errorf("the box runs as %q, want its caller %q", pod.Spec.ServiceAccountName, f.caller.Name)
	}
	if owners := pod.OwnerReferences; len(owners) != 1 || owners[0].Kind != "ServiceAccount" || owners[0].UID != account.UID {
		t.Errorf("the box is owned by %v, want its caller's ServiceAccount %s", owners, account.UID)
	}
	// The template's own labels stay: its network policy selects the box by them.
	if pod.Labels["app.kubernetes.io/name"] != "test-box" {
		t.Errorf("the box's labels %v dropped the template's", pod.Labels)
	}
}

func TestATemplateThatLetsTheWorkloadEscapeTheEgressPathIsRefused(t *testing.T) {
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
		{"without the egress sidecar", func(spec *corev1.PodSpec) { spec.Containers = spec.Containers[:1] }, true},
		{"automounting a token", func(spec *corev1.PodSpec) { spec.AutomountServiceAccountToken = nil }, true},
		{"mounting a token in the workload", tokenMountedIn(0), true},
		// The egress path itself mounts the Pod's tokens; this keeps the refusals from passing vacuously.
		{"mounting a token in the egress sidecar", tokenMountedIn(1), false},
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
	if _, err := f.inventory().Create(t.Context(), f.caller, "box", "unoffered"); !errors.As(err, &unavailable) {
		t.Errorf("Create() = %v, want an UnavailableError", err)
	}
	if _, err := f.inventory().Template(t.Context(), "unoffered"); !errors.As(err, &unavailable) {
		t.Errorf("Template() = %v, want an UnavailableError", err)
	}
}

func TestCreatingAnExistingBoxReturnsItWithoutStampingAnother(t *testing.T) {
	f := newFixture(t, nil)
	if _, err := f.inventory().Create(t.Context(), f.caller, "box", testTemplate); err != nil {
		t.Fatal(err)
	}
	first := f.pod(t, f.caller, "box")
	f.report(t, f.caller, "box", ready)
	again, err := f.inventory().Create(t.Context(), f.caller, "box", testTemplate)
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
	_, err = f.inventory().Create(t.Context(), f.caller, "box", testTemplate)
	var unavailable *UnavailableError
	if !errors.As(err, &unavailable) || !strings.Contains(unavailable.Message, "exceeded quota: test-quota") {
		t.Errorf("Create() = %v, want an UnavailableError carrying the API server's refusal", err)
	}
}

func TestAnotherCallersBoxOfThatNameIsNotReached(t *testing.T) {
	for _, test := range []struct {
		name   string
		labels func(other action.ServiceAccount) map[string]string
	}{
		{"another caller's box", func(other action.ServiceAccount) map[string]string { return boxLabels(other, "box", testTemplate) }},
		{"a Pod no Action made", func(action.ServiceAccount) map[string]string { return map[string]string{nameLabel: "box"} }},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newFixture(t, nil)
			other := account(t, f.binding.Namespace, "test-other")
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
	other := account(t, f.binding.Namespace, "test-other")
	for _, caller := range []struct {
		account action.ServiceAccount
		box     string
	}{{f.caller, "mine"}, {other, "theirs"}} {
		if _, err := f.inventory().Create(t.Context(), caller.account, caller.box, testTemplate); err != nil {
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
	if _, err := f.inventory().Create(t.Context(), f.caller, "box", testTemplate); err != nil {
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
	if _, err := f.inventory().Create(t.Context(), f.caller, "box", testTemplate); err != nil {
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
	box, err := f.inventory().Create(t.Context(), f.caller, "box", testTemplate)
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
			if _, err := f.inventory().Create(t.Context(), f.caller, "box", testTemplate); err != nil {
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
	if _, err := f.inventory().Create(t.Context(), f.caller, "box", testTemplate); err != nil {
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
