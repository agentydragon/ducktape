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

	"github.com/agentydragon/ducktape/agentplane/action_service/store"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/sandbox/contract"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/requester"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/kubernetes"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// The labels of a box. The Action Service's Pod and pods/exec grants are namespace-wide and cannot say "only the
// boxes this Action made", so the boundary is here: every read, exec and delete selects on managedLabel and on the
// caller's (callerLabel and callerNamespaceLabel for an Account, the identity labels for an Identity), and a Pod
// without them is not this surface's to act on. The Python service (binding.py, inventory.py)
// stamps the same labels, so a box either made is the other's.
const (
	labelPrefix          = "sandbox-actions.agentplane.allegedly.works/"
	managedLabel         = labelPrefix + "managed"
	callerLabel          = labelPrefix + "caller"
	callerNamespaceLabel = labelPrefix + "caller-namespace"
	identityLabel        = labelPrefix + "caller-identity"
	identityUIDLabel     = labelPrefix + "caller-identity-uid"
	templateLabel        = labelPrefix + "template"
	nameLabel            = labelPrefix + "name"
	// credentialLabel is the credential ID a box was started with, which its record must still issue for the box to
	// be bound to it.
	credentialLabel = labelPrefix + "credential"
)

// defaultContainerAnnotation names the container `kubectl exec` runs in when none is named.
const defaultContainerAnnotation = "kubectl.kubernetes.io/default-container"

// egressSidecar is the egress sidecar's container, as agentplane/egress/sidecar_config.py names it.
const egressSidecar = "egress-sidecar"

// The box's SVID, mounted into its egress sidecar alone, where the command box templates' sidecar settings read it
// (cluster/cdk8s/agentplane/command_sandbox.py, `_SVID_DIR`).
const (
	svidVolume    = "workload-svid"
	svidMountPath = "/run/agentplane/workload-svid"
)

// serviceAccountKind is the kind of a box record's subject, and of its owner when the caller has no Sandbox.
var serviceAccountKind = corev1.SchemeGroupVersion.WithKind("ServiceAccount")

// identityKind is the kind of an Identity caller's box record's subject and owner.
var identityKind = policyapi.SchemeGroupVersion.WithKind("Identity")

// defaultServiceAccount is what an Identity's box runs as: no account of its own, and no token, which every offered
// template already refuses to mount.
const defaultServiceAccount = "default"

// Caller is whom a box is for: an Account, which it runs as, or an Identity.
type Caller interface{ isCaller() }

// Account is a ServiceAccount of the group's namespace.
type Account store.ServiceAccount

// Identity is an Identity incarnation of the policy namespace, which must be the group's namespace: it owns its
// boxes' records, and an owner reference cannot cross namespaces. Its boxes run on the namespace's default
// ServiceAccount, and a later Identity of the same name reaches none of them.
type Identity store.Identity

func (Account) isCaller()  {}
func (Identity) isCaller() {}

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

// TemplateError is an offered template whose workload could read a ServiceAccount credential. A box runs as its caller, so a
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

// Inventory is every box one sandbox group owns, keyed by the caller that asked for it. A box is a plain Pod
// stamped from a PodTemplate the group offers, running as its Account caller, or on the default ServiceAccount for an
// Identity. Each box has a WorkloadCredential of its own name, subject its caller, bound to the box's Pod: its egress
// sidecar presents that SVID. The record is the box Pod's owner. An Account's Session Sandbox, else the account, owns
// the record, so a Session Sandbox's agent that created boxes loses them, and their credentials, with the Sandbox; an
// Identity's boxes go with the Identity.
type Inventory struct {
	binding catalog.PodBinding
	// policyNamespace holds the Identities; only a group of that namespace gives an Identity a box.
	policyNamespace string
	client          kubernetes.Interface
	exec            Exec
	credentials     requester.Requester
	// records reads WorkloadCredentials from the API server, uncached, to decide whether a box may be bound.
	records client.Reader
	// execGrace is how long an exec may run past its script's timeout, which `timeout` enforces in the container,
	// before this side stops waiting and reports that the script timed out.
	execGrace time.Duration
	// issuance is how long create waits for a box's credential before reporting that it is still being issued.
	issuance time.Duration
}

func NewInventory(binding catalog.PodBinding, policyNamespace string, client kubernetes.Interface, exec Exec, credentials requester.Requester, records client.Reader) *Inventory {
	return &Inventory{
		binding: binding, policyNamespace: policyNamespace, client: client, exec: exec, credentials: credentials, records: records,
		execGrace: 15 * time.Second, issuance: 30 * time.Second,
	}
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
	// The box's SVID is mounted where this service puts it and nowhere else, so a template cannot claim it for the
	// workload by naming its volume.
	reserved := func(mount corev1.VolumeMount) bool {
		return mount.Name == svidVolume || mount.MountPath == svidMountPath
	}
	if slices.ContainsFunc(spec.Volumes, func(volume corev1.Volume) bool { return volume.Name == svidVolume }) ||
		slices.ContainsFunc(slices.Concat(spec.InitContainers, spec.Containers), func(container corev1.Container) bool {
			return slices.ContainsFunc(container.VolumeMounts, reserved)
		}) {
		return &TemplateError{name, fmt.Sprintf("defines the reserved SVID volume %q or mount %q", svidVolume, svidMountPath)}
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
// would expose a credential to the workload, is a deployment defect, and refusing to start names it where a caller would only meet
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
func objectName(caller Caller, name string) string {
	var prefix string
	switch caller := caller.(type) {
	case Account:
		prefix = caller.Name
	case Identity:
		prefix = caller.Name
	default:
		panic(fmt.Sprintf("unknown caller %T", caller))
	}
	object := prefix + "-" + name
	return strings.TrimRight(object[:min(len(object), 63)], "-")
}

// callerLabels are the labels that say whose a box is. A box carries one caller kind's labels and none of the other's.
func callerLabels(caller Caller) map[string]string {
	switch caller := caller.(type) {
	case Account:
		return map[string]string{callerLabel: caller.Name, callerNamespaceLabel: caller.Namespace}
	case Identity:
		return map[string]string{identityLabel: caller.Name, identityUIDLabel: caller.UID}
	default:
		panic(fmt.Sprintf("unknown caller %T", caller))
	}
}

// callers reports whether a box's labels name caller, and no caller of another kind.
func callers(boxLabels map[string]string, caller Caller) bool {
	want := callerLabels(caller)
	for _, key := range []string{callerLabel, callerNamespaceLabel, identityLabel, identityUIDLabel} {
		if boxLabels[key] != want[key] {
			return false
		}
	}
	return true
}

func boxLabels(caller Caller, name, template string) map[string]string {
	labels := map[string]string{managedLabel: "true", templateLabel: template, nameLabel: name}
	maps.Copy(labels, callerLabels(caller))
	return labels
}

// pod is the caller's box of that name, or nil. A same-named Pod this surface did not create for the caller is a
// ForeignError rather than adopted or reported absent.
func (i *Inventory) pod(ctx context.Context, caller Caller, name string) (*corev1.Pod, error) {
	pod, err := i.client.CoreV1().Pods(i.binding.Namespace).Get(ctx, objectName(caller, name), metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if pod.Labels[managedLabel] != "true" || !callers(pod.Labels, caller) {
		return nil, &ForeignError{name}
	}
	return pod, nil
}

// Create starts the caller's box if it has none by that name, and reports where it got to. It returns once the Pod
// exists rather than once it is ready: an image pull can outlast the Execution's lease, so waiting here would
// report an unknown outcome for a box that is fine. Get is how a caller follows one to ready. A new box waits for its
// credential first, which its Pod must name when it is created.
//
// An existing box of that name is returned as it stands, whatever template it was created from: deciding that its
// shape is wrong is the caller's, and Dispose is how it acts on that.
func (i *Inventory) Create(ctx context.Context, caller Caller, name, template string) (Info, error) {
	box, err := i.Template(ctx, template)
	if err != nil {
		return Info{}, err
	}
	var account *corev1.ServiceAccount
	if caller, ok := caller.(Account); ok {
		if account, err = i.client.CoreV1().ServiceAccounts(i.binding.Namespace).Get(ctx, caller.Name, metav1.GetOptions{}); err != nil {
			return Info{}, err
		}
	}
	existing, err := i.pod(ctx, caller, name)
	switch {
	case err != nil:
		return Info{}, err
	case existing == nil:
		if err := i.stamp(ctx, caller, account, name, template, box); err != nil {
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
	// Also a box another replica stamped and may not have lived to bind.
	if err := i.bind(ctx, caller, account, pod); err != nil {
		return Info{}, err
	}
	return info(pod), nil
}

// credentialOwner is the request for a box's credential: the record takes the box's name and outlives nothing its
// caller does not. An Account's record is owned by the account's controller, a Session Sandbox, else the account
// itself, so a Sandbox's boxes go with the Sandbox rather than with the account SANDBOX_IDENTITY_SPEC stops creating;
// its subject is the account. An Identity's record names and is owned by the Identity incarnation, so deleting the
// Identity takes its boxes. account is the Account caller's ServiceAccount, nil for an Identity.
func credentialOwner(namespace string, caller Caller, account *corev1.ServiceAccount, name string) requester.Request {
	var owner metav1.OwnerReference
	var subject requester.Subject
	switch caller := caller.(type) {
	case Account:
		owner = metav1.OwnerReference{APIVersion: serviceAccountKind.GroupVersion().String(), Kind: serviceAccountKind.Kind, Name: account.Name, UID: account.UID}
		if controller := metav1.GetControllerOfNoCopy(account); controller != nil {
			// Without blockOwnerDeletion, which would need the Sandbox's finalizers.
			owner = metav1.OwnerReference{APIVersion: controller.APIVersion, Kind: controller.Kind, Name: controller.Name, UID: controller.UID}
		}
		subject = requester.ServiceAccount{Name: account.Name}
	case Identity:
		owner = metav1.OwnerReference{APIVersion: identityKind.GroupVersion().String(), Kind: identityKind.Kind, Name: caller.Name, UID: types.UID(caller.UID)}
		subject = requester.Identity{Name: caller.Name, UID: types.UID(caller.UID)}
	default:
		panic(fmt.Sprintf("unknown caller %T", caller))
	}
	owner.Controller = new(true)
	return requester.Request{
		Owner:   requester.Owner{Namespace: namespace, Name: objectName(caller, name), Reference: owner},
		Subject: subject,
	}
}

// issue waits until the box's record issues an unbound credential, the one a new Pod starts with: a record still
// bound to an earlier Pod of the name mints a fresh one. It reports false if a box of the name appeared meanwhile,
// another replica's, whose credential the next unbound request would revoke.
func (i *Inventory) issue(ctx context.Context, caller Caller, request requester.Request, name string) (requester.Issued, bool, error) {
	var issued requester.Issued
	var pending requester.Pending
	appeared := false
	err := wait.PollUntilContextTimeout(ctx, 250*time.Millisecond, i.issuance, true, func(ctx context.Context) (bool, error) {
		switch pod, err := i.pod(ctx, caller, name); {
		case err != nil:
			return false, err
		case pod != nil:
			appeared = true
			return true, nil
		}
		credential, err := i.credentials.Ensure(ctx, request)
		if err != nil {
			return false, err
		}
		switch credential := credential.(type) {
		case requester.Issued:
			issued = credential
			return true, nil
		case requester.Pending:
			pending = credential
			return false, nil
		default:
			return false, fmt.Errorf("unknown credential %T", credential)
		}
	})
	if wait.Interrupted(err) && ctx.Err() == nil {
		return requester.Issued{}, false, &UnavailableError{fmt.Sprintf("the credential of sandbox %q is still being issued (%s: %s); create it again",
			name, pending.Reason, pending.Message)}
	}
	return issued, !appeared, err
}

func (i *Inventory) stamp(ctx context.Context, caller Caller, account *corev1.ServiceAccount, name, templateName string, template *corev1.PodTemplateSpec) error {
	request := credentialOwner(i.binding.Namespace, caller, account, name)
	issued, fresh, err := i.issue(ctx, caller, request, name)
	if err != nil || !fresh {
		return err
	}
	var record workloadapi.WorkloadCredential
	if err := i.records.Get(ctx, types.NamespacedName{Namespace: request.Owner.Namespace, Name: request.Owner.Name}, &record); err != nil {
		return err
	}
	if record.Status.CredentialID != issued.ID {
		return &UnavailableError{fmt.Sprintf("the credential of sandbox %q changed as it was created; create it again", name)}
	}
	spec := *template.Spec.DeepCopy()
	switch caller := caller.(type) {
	case Account:
		// CLEANUP(added 2026-11-09): an Account's box keeps running as it, whose Kubernetes-audience token its sidecar
		// substitutes for `kubectl`, until IDENTITY_CUTOVER (agentplane/plans/task_dag.md) makes every caller an
		// Identity.
		spec.ServiceAccountName = caller.Name
	case Identity:
		spec.ServiceAccountName = defaultServiceAccount
	}
	withCredential(&spec, issued.SecretName)
	podLabels := maps.Clone(template.Labels)
	if podLabels == nil {
		podLabels = map[string]string{}
	}
	maps.Copy(podLabels, boxLabels(caller, name, templateName))
	podLabels[credentialLabel] = issued.ID
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:        objectName(caller, name),
			Labels:      podLabels,
			Annotations: template.Annotations,
			// Collected with its record, so disposing or collecting the record stops the box too. The record has no
			// owner reference to the Pod: the Pod must name the record's Secret when it is created.
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: workloadapi.SchemeGroupVersion.String(), Kind: workloadapi.Kind,
				Name: record.Name, UID: record.UID, Controller: new(true),
			}},
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

// withCredential mounts the credential's Secret into the egress sidecar, which presents it; no other container sees
// it. A box without a sidecar has nothing that could present it.
func withCredential(spec *corev1.PodSpec, secretName string) {
	mount := corev1.VolumeMount{Name: svidVolume, MountPath: svidMountPath, ReadOnly: true}
	mounted := false
	for _, containers := range [][]corev1.Container{spec.InitContainers, spec.Containers} {
		for n := range containers {
			if containers[n].Name == egressSidecar {
				containers[n].VolumeMounts = append(containers[n].VolumeMounts, mount)
				mounted = true
			}
		}
	}
	if !mounted {
		return
	}
	// Readable by the sidecar's group, the Pod's fsGroup.
	mode := int32(0o440)
	spec.Volumes = append(spec.Volumes, corev1.Volume{
		Name: svidVolume,
		VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{
			SecretName:  secretName,
			Items:       []corev1.KeyToPath{{Key: corev1.TLSCertKey, Path: corev1.TLSCertKey}, {Key: corev1.TLSPrivateKeyKey, Path: corev1.TLSPrivateKeyKey}},
			DefaultMode: &mode,
		}},
	})
}

// bind accepts pod as the incarnation of the credential it was started with: the first binding after a fresh ID
// accepts that ID. Only the record that owns the Pod, still issuing the Pod's credential and bound to no other Pod,
// is bound; binding one that has since rotated would accept an ID the Pod does not hold.
func (i *Inventory) bind(ctx context.Context, caller Caller, account *corev1.ServiceAccount, pod *corev1.Pod) error {
	request := credentialOwner(i.binding.Namespace, caller, account, pod.Labels[nameLabel])
	var record workloadapi.WorkloadCredential
	if err := i.records.Get(ctx, types.NamespacedName{Namespace: request.Owner.Namespace, Name: request.Owner.Name}, &record); apierrors.IsNotFound(err) {
		return nil
	} else if err != nil {
		return err
	}
	owner := metav1.GetControllerOfNoCopy(pod)
	if owner == nil || owner.UID != record.UID || record.Status.CredentialID != pod.Labels[credentialLabel] ||
		(record.Spec.Binding != nil && record.Spec.Binding.PodUID != pod.UID) {
		return nil
	}
	request.Binding = requester.Pod{Name: pod.Name, UID: pod.UID}
	_, err := i.credentials.Ensure(ctx, request)
	return err
}

// Get is the caller's box of that name.
func (i *Inventory) Get(ctx context.Context, caller Caller, name string) (Info, error) {
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
func (i *Inventory) List(ctx context.Context, caller Caller) ([]Info, error) {
	selector := labels.Set{managedLabel: "true"}
	maps.Copy(selector, callerLabels(caller))
	pods, err := i.client.CoreV1().Pods(i.binding.Namespace).List(ctx, metav1.ListOptions{
		LabelSelector: labels.SelectorFromSet(selector).String(),
	})
	if err != nil {
		return nil, err
	}
	boxes := make([]Info, 0, len(pods.Items))
	for n := range pods.Items {
		// The selector cannot say that the other kind's labels are absent.
		if callers(pods.Items[n].Labels, caller) {
			boxes = append(boxes, info(&pods.Items[n]))
		}
	}
	return boxes, nil
}

// Dispose revokes the caller's box's credential and deletes the box; false when it had none to delete. The
// credential goes first, so the box's SVID stops authenticating before its Pod has stopped.
func (i *Inventory) Dispose(ctx context.Context, caller Caller, name string) (bool, error) {
	pod, err := i.pod(ctx, caller, name)
	if err != nil || pod == nil {
		return false, err
	}
	if err := i.revoke(ctx, pod); err != nil {
		return false, err
	}
	err = i.client.CoreV1().Pods(i.binding.Namespace).Delete(ctx, pod.Name, metav1.DeleteOptions{})
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	return err == nil, err
}

// revoke deletes the record pod runs under, if it still does: a record of the name that does not own pod is another
// box's.
func (i *Inventory) revoke(ctx context.Context, pod *corev1.Pod) error {
	var record workloadapi.WorkloadCredential
	if err := i.records.Get(ctx, types.NamespacedName{Namespace: pod.Namespace, Name: pod.Name}, &record); apierrors.IsNotFound(err) {
		return nil
	} else if err != nil {
		return err
	}
	box, owner := metav1.GetControllerOfNoCopy(pod), metav1.GetControllerOfNoCopy(&record)
	if box == nil || box.UID != record.UID || owner == nil {
		return nil
	}
	return i.credentials.Revoke(ctx, requester.Owner{Namespace: record.Namespace, Name: record.Name, Reference: *owner})
}

// Run runs script in the caller's box, which must be ready. The caps are this group's at most: submission already
// refuses past them, and a request another replica admitted under other caps, across a settings roll, is still held
// to these.
func (i *Inventory) Run(ctx context.Context, caller Caller, name string, run Script) (ExecResult, error) {
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
		if pod.Status.Conditions[n].Type == contract.ReadyCondition {
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
