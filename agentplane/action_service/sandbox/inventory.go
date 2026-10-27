package sandbox

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
)

// The labels of a box. The Action Service's Pod and pods/exec grants are namespace-wide and cannot say "only the
// boxes this Action made", so the boundary is here: every read, exec and delete selects on managedLabel and on the
// caller's, and a Pod without them is not this surface's to act on. The Python service (binding.py, inventory.py)
// stamps the same labels, so a box either made is the other's.
const (
	labelPrefix          = "sandbox-actions.agentplane.allegedly.works/"
	managedLabel         = labelPrefix + "managed"
	callerLabel          = labelPrefix + "caller"
	callerNamespaceLabel = labelPrefix + "caller-namespace"
	templateLabel        = labelPrefix + "template"
	nameLabel            = labelPrefix + "name"
)

// defaultContainerAnnotation names the container `kubectl exec` runs in when none is named.
const defaultContainerAnnotation = "kubectl.kubernetes.io/default-container"

// egressSidecar is the egress sidecar's container, as agentplane/egress/sidecar_config.py names it.
const egressSidecar = "egress-sidecar"

// UnavailableError is a request the inventory cannot carry out, in terms the caller can act on.
type UnavailableError struct {
	Message string
}

func (e *UnavailableError) Error() string { return e.Message }

// ForeignError is a Pod of the box's name that this surface did not create for the caller.
type ForeignError struct {
	Name string
}

func (e *ForeignError) Error() string {
	return fmt.Sprintf("a sandbox named %q exists and is not yours", e.Name)
}

// TemplateError is an offered template whose box would escape the egress path. A box runs as its caller, so a
// ServiceAccount token in a container an agent runs commands in would hand out the caller's whole authority as a
// portable credential.
type TemplateError struct {
	Template string
	Problem  string
}

func (e *TemplateError) Error() string {
	return fmt.Sprintf("box template %q %s", e.Template, e.Problem)
}

// Info is the non-secret state of one box, as the API server reports its Pod.
type Info struct {
	Name     string `json:"name"`
	Template string `json:"template"`
	// Phase is absent until Kubernetes reports one. "Failed" is a box evicted from its node, which only dispose
	// and a new create recover.
	Phase *string `json:"phase"`
	// Conditions are the Pod's, verbatim: "Ready" with status "True" is what exec requires, and until then that
	// condition says why.
	Conditions []Condition `json:"conditions"`
	CreatedAt  *time.Time  `json:"created_at"`
	NodeName   *string     `json:"node_name"`
	PodIPs     []string    `json:"pod_ips"`
}

// Condition is one condition of a box's Pod, as Kubernetes reports it.
type Condition struct {
	Type               string     `json:"type"`
	Status             string     `json:"status"`
	Reason             *string    `json:"reason"`
	Message            *string    `json:"message"`
	LastTransitionTime *time.Time `json:"last_transition_time"`
}

// Inventory is every box one sandbox group owns, keyed by the caller that asked for it. A box is a plain Pod that
// runs as its caller, stamped from a PodTemplate the group offers, and owned by its caller's ServiceAccount, so a
// Session Sandbox's agent that created boxes loses them with the Sandbox.
type Inventory struct {
	binding catalog.PodBinding
	client  kubernetes.Interface
	exec    Exec
	// execGrace is how long an exec may run past its script's timeout, which `timeout` enforces in the container,
	// before this side stops waiting and reports that the script timed out.
	execGrace time.Duration
}

func NewInventory(binding catalog.PodBinding, client kubernetes.Interface, exec Exec) *Inventory {
	return &Inventory{binding: binding, client: client, exec: exec, execGrace: 15 * time.Second}
}

// Template is the Pod an offered template gives each box, read now: the image pins move under a running service.
// Every caller may read all of it, which is why a template references Secrets and never carries one.
func (i *Inventory) Template(ctx context.Context, name string) (*corev1.PodTemplateSpec, error) {
	if _, offered := i.binding.Templates[name]; !offered {
		return nil, &UnavailableError{fmt.Sprintf("unknown template %q; this deployment offers %s", name,
			strings.Join(slices.Sorted(maps.Keys(i.binding.Templates)), ", "))}
	}
	template, err := i.client.CoreV1().PodTemplates(i.binding.Namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	if err := checkTemplate(name, &template.Template); err != nil {
		return nil, err
	}
	return &template.Template, nil
}

func checkTemplate(name string, template *corev1.PodTemplateSpec) error {
	spec := template.Spec
	if spec.AutomountServiceAccountToken == nil || *spec.AutomountServiceAccountToken {
		return &TemplateError{name, "must set automountServiceAccountToken: false"}
	}
	if !slices.ContainsFunc(spec.Containers, func(c corev1.Container) bool { return c.Name == egressSidecar }) {
		return &TemplateError{name, fmt.Sprintf("must carry the %q container", egressSidecar)}
	}
	tokenVolumes := map[string]bool{}
	for _, volume := range spec.Volumes {
		if volume.Projected != nil && slices.ContainsFunc(volume.Projected.Sources,
			func(source corev1.VolumeProjection) bool { return source.ServiceAccountToken != nil }) {
			tokenVolumes[volume.Name] = true
		}
	}
	for _, container := range spec.Containers {
		if container.Name == egressSidecar {
			continue
		}
		var mounted []string
		for _, mount := range container.VolumeMounts {
			if tokenVolumes[mount.Name] {
				mounted = append(mounted, mount.Name)
			}
		}
		if len(mounted) > 0 {
			slices.Sort(mounted)
			return &TemplateError{name, fmt.Sprintf("mounts ServiceAccount tokens %q in %q", slices.Compact(mounted), container.Name)}
		}
	}
	return nil
}

// CheckTemplates reads every offered template once, when the service starts: one that is missing, or whose box
// would escape the egress path, is a deployment defect, and refusing to start names it where a caller would only meet
// it as a box that cannot be created.
func (i *Inventory) CheckTemplates(ctx context.Context) error {
	for _, name := range slices.Sorted(maps.Keys(i.binding.Templates)) {
		// The API server's errors name the template, as TemplateError does.
		if _, err := i.Template(ctx, name); err != nil {
			return err
		}
	}
	return nil
}

// objectName is a box's Pod's name: the same for the same caller and name, so creating a box twice reaches the
// same Pod.
func objectName(caller action.ServiceAccount, name string) string {
	object := caller.Name + "-" + name
	return strings.TrimRight(object[:min(len(object), 63)], "-")
}

func boxLabels(caller action.ServiceAccount, name, template string) map[string]string {
	return map[string]string{
		managedLabel:         "true",
		callerLabel:          caller.Name,
		callerNamespaceLabel: caller.Namespace,
		templateLabel:        template,
		nameLabel:            name,
	}
}

// pod is the caller's box of that name, or nil. A same-named Pod this surface did not create for the caller is a
// ForeignError rather than adopted or reported absent.
func (i *Inventory) pod(ctx context.Context, caller action.ServiceAccount, name string) (*corev1.Pod, error) {
	pod, err := i.client.CoreV1().Pods(i.binding.Namespace).Get(ctx, objectName(caller, name), metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if pod.Labels[managedLabel] != "true" || pod.Labels[callerLabel] != caller.Name ||
		pod.Labels[callerNamespaceLabel] != caller.Namespace {
		return nil, &ForeignError{name}
	}
	return pod, nil
}

// Create starts the caller's box if it has none by that name, and reports where it got to. It returns once the Pod
// exists rather than once it is ready: an image pull can outlast the Execution's lease, so waiting here would
// report an unknown outcome for a box that is fine. Get is how a caller follows one to ready.
//
// An existing box of that name is returned as it stands, whatever template it was created from: deciding that its
// shape is wrong is the caller's, and Dispose is how it acts on that.
func (i *Inventory) Create(ctx context.Context, caller action.ServiceAccount, name, template string) (Info, error) {
	box, err := i.Template(ctx, template)
	if err != nil {
		return Info{}, err
	}
	existing, err := i.pod(ctx, caller, name)
	switch {
	case err != nil:
		return Info{}, err
	case existing == nil:
		if err := i.stamp(ctx, caller, name, template, box); err != nil {
			return Info{}, err
		}
	case existing.Labels[templateLabel] != template:
		slog.Info("sandbox exists from another template; returning it as it stands",
			"name", name, "template", existing.Labels[templateLabel])
	}
	pod, err := i.pod(ctx, caller, name)
	if err != nil {
		return Info{}, err
	}
	if pod == nil {
		return Info{}, &UnavailableError{fmt.Sprintf("sandbox %q disappeared as it was created", name)}
	}
	return info(pod), nil
}

func (i *Inventory) stamp(ctx context.Context, caller action.ServiceAccount, name, templateName string, template *corev1.PodTemplateSpec) error {
	account, err := i.client.CoreV1().ServiceAccounts(i.binding.Namespace).Get(ctx, caller.Name, metav1.GetOptions{})
	if err != nil {
		return err
	}
	spec := template.Spec
	spec.ServiceAccountName = caller.Name
	podLabels := maps.Clone(template.Labels)
	if podLabels == nil {
		podLabels = map[string]string{}
	}
	maps.Copy(podLabels, boxLabels(caller, name, templateName))
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:        objectName(caller, name),
			Labels:      podLabels,
			Annotations: template.Annotations,
			// Collected with the account: a Session Sandbox's account goes with the Sandbox, and so do the boxes
			// its agent made.
			OwnerReferences: []metav1.OwnerReference{{APIVersion: "v1", Kind: "ServiceAccount", Name: caller.Name, UID: account.UID}},
		},
		// TODO(sandbox-lifetime): a forgotten box keeps its share of the namespace's quota until someone disposes of
		// it. Expire boxes nobody has exec'd into for a while.
		Spec: spec,
	}
	_, err = i.client.CoreV1().Pods(i.binding.Namespace).Create(ctx, pod, metav1.CreateOptions{})
	var refusal apierrors.APIStatus
	if !errors.As(err, &refusal) {
		return err
	}
	switch status := refusal.Status(); status.Code {
	case 409:
		// Another replica stamped the same box; its Pod is as good as this one.
		return nil
	case 403, 422:
		return &UnavailableError{fmt.Sprintf("the API server refused sandbox %q (%d %s): %s", name, status.Code, status.Reason, status.Message)}
	}
	return err
}

// Get is the caller's box of that name.
func (i *Inventory) Get(ctx context.Context, caller action.ServiceAccount, name string) (Info, error) {
	pod, err := i.pod(ctx, caller, name)
	if err != nil {
		return Info{}, err
	}
	if pod == nil {
		return Info{}, &UnavailableError{fmt.Sprintf("no sandbox named %q; create it first", name)}
	}
	return info(pod), nil
}

// List is every box of the caller's.
func (i *Inventory) List(ctx context.Context, caller action.ServiceAccount) ([]Info, error) {
	pods, err := i.client.CoreV1().Pods(i.binding.Namespace).List(ctx, metav1.ListOptions{
		LabelSelector: labels.SelectorFromSet(labels.Set{
			managedLabel: "true", callerLabel: caller.Name, callerNamespaceLabel: caller.Namespace,
		}).String(),
	})
	if err != nil {
		return nil, err
	}
	boxes := make([]Info, len(pods.Items))
	for n := range pods.Items {
		boxes[n] = info(&pods.Items[n])
	}
	return boxes, nil
}

// Dispose deletes the caller's box; false when it had none to delete.
func (i *Inventory) Dispose(ctx context.Context, caller action.ServiceAccount, name string) (bool, error) {
	pod, err := i.pod(ctx, caller, name)
	if err != nil || pod == nil {
		return false, err
	}
	err = i.client.CoreV1().Pods(i.binding.Namespace).Delete(ctx, pod.Name, metav1.DeleteOptions{})
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	return err == nil, err
}

// Run runs script in the caller's box, which must be ready. The caps are this group's at most: submission already
// refuses past them, and a request another replica admitted under other caps, across a settings roll, is still held
// to these.
func (i *Inventory) Run(ctx context.Context, caller action.ServiceAccount, name string, run Script) (ExecResult, error) {
	pod, err := i.pod(ctx, caller, name)
	if err != nil {
		return ExecResult{}, err
	}
	if pod == nil {
		return ExecResult{}, &UnavailableError{fmt.Sprintf("no sandbox named %q; create it first", name)}
	}
	ready := readiness(pod)
	if ready == nil || ready.Status != corev1.ConditionTrue {
		said := "Kubernetes has not said why"
		if ready != nil {
			said = cmp.Or(ready.Message, ready.Reason, said)
		}
		return ExecResult{}, &UnavailableError{fmt.Sprintf("sandbox %q is not ready (%s)", name, said)}
	}
	run.TimeoutSeconds = min(run.TimeoutSeconds, i.binding.MaxTimeoutSeconds)
	run.MaxOutputBytes = min(run.MaxOutputBytes, i.binding.MaxOutputBytes)
	return run.in(ctx, i.exec, types.NamespacedName{Namespace: pod.Namespace, Name: pod.Name}, workloadContainer(pod), i.execGrace)
}

func readiness(pod *corev1.Pod) *corev1.PodCondition {
	for n := range pod.Status.Conditions {
		if pod.Status.Conditions[n].Type == readyCondition {
			return &pod.Status.Conditions[n]
		}
	}
	return nil
}

// workloadContainer is the container commands run in, by the rule `kubectl exec` follows: the one the Pod names as
// its default, else its first.
func workloadContainer(pod *corev1.Pod) string {
	if container := pod.Annotations[defaultContainerAnnotation]; container != "" {
		return container
	}
	return pod.Spec.Containers[0].Name
}

func info(pod *corev1.Pod) Info {
	conditions := make([]Condition, len(pod.Status.Conditions))
	for n, condition := range pod.Status.Conditions {
		conditions[n] = Condition{
			Type:               string(condition.Type),
			Status:             string(condition.Status),
			Reason:             optional(condition.Reason),
			Message:            optional(condition.Message),
			LastTransitionTime: optionalTime(condition.LastTransitionTime),
		}
	}
	addresses := make([]string, len(pod.Status.PodIPs))
	for n, address := range pod.Status.PodIPs {
		addresses[n] = address.IP
	}
	return Info{
		Name:       pod.Labels[nameLabel],
		Template:   pod.Labels[templateLabel],
		Phase:      optional(string(pod.Status.Phase)),
		Conditions: conditions,
		CreatedAt:  optionalTime(pod.CreationTimestamp),
		NodeName:   optional(pod.Spec.NodeName),
		PodIPs:     addresses,
	}
}

// optional is s, or nil for the empty string a Kubernetes field holds when it is not set.
func optional(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// optionalTime is t in UTC, as the API server wrote it, or nil when it is not set.
func optionalTime(t metav1.Time) *time.Time {
	if t.IsZero() {
		return nil
	}
	return new(t.UTC())
}
