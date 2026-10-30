// Package sandboxes provides the app's caller-scoped access to the namespaced Sandbox inventory.
package sandboxes

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"unicode/utf8"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/durationpb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	CreateIntentAnnotation = "agentplane.allegedly.works/create-intent"
	PresetLabel            = "agentplane.allegedly.works/preset"
	maxBootstrapCharacters = 65_536
	mutationAttempts       = 5
)

// Access is the app's namespace-scoped Sandbox service. Its caller and default policy sets are
// fixed at construction, as they are for the app process identity.
type Access struct {
	namespace        string
	objects          client.Client
	reader           *sandboxreader.Reader
	defaultPolicySet map[string]struct{}
	caller           *sandboxespb.ServiceAccount
}

// New creates access over one Sandbox namespace and app caller identity.
func New(
	namespace string,
	objects client.Client,
	defaultPolicySets []string,
	caller *sandboxespb.ServiceAccount,
) (*Access, error) {
	if namespace == "" || objects == nil || caller == nil || caller.Namespace == "" || caller.Name == "" {
		return nil, errors.New("Sandbox namespace, Kubernetes client, and caller ServiceAccount are required")
	}
	reader, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	defaults := make(map[string]struct{}, len(defaultPolicySets))
	for _, name := range defaultPolicySets {
		defaults[name] = struct{}{}
	}
	return &Access{
		namespace: namespace, objects: objects, reader: reader,
		defaultPolicySet: defaults, caller: &sandboxespb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name},
	}, nil
}

// ListSandboxes lists Sandbox resources and joins only the Pod UID reported as each Sandbox's
// current incarnation.
func (a *Access) ListSandboxes(ctx context.Context) ([]*sandboxespb.Sandbox, error) {
	var sandboxes sandboxapi.SandboxList
	if err := a.objects.List(ctx, &sandboxes, client.InNamespace(a.namespace)); err != nil {
		return nil, fmt.Errorf("list Sandboxes: %w", err)
	}
	var pods corev1PodList
	if err := a.objects.List(ctx, &pods, client.InNamespace(a.namespace)); err != nil {
		return nil, fmt.Errorf("list Sandbox Pods: %w", err)
	}
	podsByName := make(map[string]*corev1Pod, len(pods.Items))
	for index := range pods.Items {
		pod := &pods.Items[index]
		podsByName[pod.Name] = pod
	}
	views := make([]*sandboxespb.Sandbox, 0, len(sandboxes.Items))
	for index := range sandboxes.Items {
		resource := &sandboxes.Items[index]
		var pod *corev1Pod
		if incarnation := resource.Status.Incarnation; incarnation != nil {
			candidate := podsByName[incarnation.Name]
			if candidate != nil && string(candidate.UID) == incarnation.UID {
				pod = candidate
			}
		}
		view, err := sandboxView(resource, pod)
		if err != nil {
			return nil, fmt.Errorf("view Sandbox %q: %w", resource.Name, err)
		}
		views = append(views, view)
	}
	sort.Slice(views, func(i, j int) bool { return views[i].Name < views[j].Name })
	return views, nil
}

// ListTemplates returns the sorted names of templates in the Sandbox namespace.
func (a *Access) ListTemplates(ctx context.Context) ([]string, error) {
	var templates sandboxapi.SandboxTemplateList
	if err := a.objects.List(ctx, &templates, client.InNamespace(a.namespace)); err != nil {
		return nil, fmt.Errorf("list SandboxTemplates: %w", err)
	}
	names := make([]string, 0, len(templates.Items))
	for _, template := range templates.Items {
		names = append(names, template.Name)
	}
	sort.Strings(names)
	return names, nil
}

// Get returns one Sandbox view and its reported Pod incarnation, if the Pod still has that UID.
func (a *Access) Get(ctx context.Context, name string) (*sandboxespb.Sandbox, error) {
	resource, err := a.read(ctx, name)
	if err != nil {
		return nil, err
	}
	var pod *corev1Pod
	if incarnation := resource.Status.Incarnation; incarnation != nil {
		candidate := &corev1Pod{}
		err := a.objects.Get(ctx, types.NamespacedName{Namespace: a.namespace, Name: incarnation.Name}, candidate)
		if err != nil && !apierrors.IsNotFound(err) {
			return nil, fmt.Errorf("read Sandbox incarnation Pod: %w", err)
		}
		if err == nil && string(candidate.UID) == incarnation.UID {
			pod = candidate
		}
	}
	return sandboxView(resource, pod)
}

// Create validates and creates a running Sandbox, applying the process defaults and recording an
// immutable caller/request intent so an identical retry is idempotent.
func (a *Access) Create(ctx context.Context, request *sandboxespb.CreateSandboxRequest) (*sandboxespb.Sandbox, error) {
	if err := validateCreate(request); err != nil {
		return nil, err
	}
	intent, err := createIntent(request, a.caller)
	if err != nil {
		return nil, &InvalidSandboxError{Reason: "create request cannot be recorded"}
	}
	if existing, err := a.retry(ctx, request.GetName(), intent); err != nil {
		return nil, err
	} else if existing != nil {
		return a.Get(ctx, request.GetName())
	}

	policySets := make(map[string]struct{}, len(a.defaultPolicySet)+len(request.GetPolicySets()))
	for name := range a.defaultPolicySet {
		policySets[name] = struct{}{}
	}
	for _, name := range request.GetPolicySets() {
		policySets[name] = struct{}{}
	}
	policySetNames := sortedKeys(policySets)
	if err := a.requirePolicySets(ctx, policySetNames); err != nil {
		return nil, err
	}

	resource := sandboxForCreate(request, a.namespace, policySetNames, intent)
	if err := a.objects.Create(ctx, resource); err != nil {
		existing, retryErr := a.retry(ctx, request.GetName(), intent)
		if retryErr != nil {
			return nil, retryErr
		}
		if existing == nil {
			return nil, err
		}
	}
	return a.Get(ctx, request.GetName())
}

// Suspend requests that the exact current Sandbox stop. It does not wait for controller status.
func (a *Access) Suspend(ctx context.Context, name string) error {
	view, err := a.Get(ctx, name)
	if err != nil {
		return err
	}
	return a.setRunning(ctx, name, view.GetUid(), false)
}

// Resume requests that the exact current Sandbox run. A controller-reported idle suspension is
// woken by advancing wakeGeneration exactly once.
func (a *Access) Resume(ctx context.Context, name string) error {
	view, err := a.Get(ctx, name)
	if err != nil {
		return err
	}
	return a.setRunning(ctx, name, view.GetUid(), true)
}

// SetWakeOnSend changes the durable wake policy on the UID selected by the caller.
func (a *Access) SetWakeOnSend(ctx context.Context, name, uid string, wakeOnSend bool) error {
	return a.retryConflict(func() error {
		resource, err := a.incarnation(ctx, name, uid)
		if err != nil {
			return err
		}
		if resource.DeletionTimestamp != nil {
			return &SandboxTearingDownError{Name: name}
		}
		if resource.Spec.WakeOnSend == wakeOnSend {
			return nil
		}
		return a.patchSpec(ctx, resource, map[string]any{"wakeOnSend": wakeOnSend})
	})
}

// SetIdleSuspendAfter changes the idle suspend policy on the UID selected by the caller. A nil
// duration clears the policy.
func (a *Access) SetIdleSuspendAfter(ctx context.Context, name, uid string, value *durationpb.Duration) error {
	var duration *metav1.Duration
	if value != nil {
		if err := value.CheckValid(); err != nil {
			return &InvalidSandboxError{Reason: "idle suspend duration is invalid"}
		}
		duration = &metav1.Duration{Duration: value.AsDuration()}
	}
	return a.retryConflict(func() error {
		resource, err := a.incarnation(ctx, name, uid)
		if err != nil {
			return err
		}
		if resource.DeletionTimestamp != nil {
			return &SandboxTearingDownError{Name: name}
		}
		if sameDuration(resource.Spec.IdleSuspendAfter, duration) {
			return nil
		}
		if duration == nil {
			return a.patchSpec(ctx, resource, map[string]any{"idleSuspendAfter": nil})
		}
		return a.patchSpec(ctx, resource, map[string]any{"idleSuspendAfter": duration.Duration.String()})
	})
}

// Delete begins teardown of the exact current Sandbox. Running Sandboxes are refused; repeated
// requests against a Sandbox already deleting are idempotent.
func (a *Access) Delete(ctx context.Context, name string) error {
	view, err := a.Get(ctx, name)
	if err != nil {
		return err
	}
	uid := view.GetUid()
	return a.retryConflict(func() error {
		resource, err := a.incarnation(ctx, name, uid)
		if err != nil {
			return err
		}
		return a.beginTeardown(ctx, resource, name, uid)
	})
}

func (a *Access) setRunning(ctx context.Context, name, uid string, running bool) error {
	return a.retryConflict(func() error {
		resource, err := a.incarnation(ctx, name, uid)
		if err != nil {
			return err
		}
		if resource.DeletionTimestamp != nil {
			return &SandboxTearingDownError{Name: name}
		}
		if running && resource.Status.StopStage == sandboxapi.StopStageQuiescing {
			return &SandboxQuiescingError{Name: name}
		}
		if running {
			if resource.Status.OperatingMode == sandboxapi.OperatingModeSuspended {
				return a.requestResume(ctx, resource, name, uid)
			}
			if resource.Spec.Running {
				return nil
			}
		}
		return a.patchSpec(ctx, resource, map[string]any{"running": running})
	})
}

func (a *Access) requestResume(ctx context.Context, resource *sandboxapi.Sandbox, name, uid string) error {
	if resource.Status.StopStage == sandboxapi.StopStageQuiescing {
		return &SandboxQuiescingError{Name: name}
	}
	idleStillSuspended := resource.Status.SuspendedBy != nil &&
		*resource.Status.SuspendedBy == sandboxapi.SuspendedByIdle &&
		resource.Status.SuspendedAtWakeGeneration != nil &&
		*resource.Status.SuspendedAtWakeGeneration == resource.Spec.WakeGeneration
	if resource.Spec.Running && !idleStillSuspended {
		return nil
	}
	return a.patchSpec(ctx, resource, map[string]any{
		"running":        true,
		"wakeGeneration": resource.Spec.WakeGeneration + 1,
	})
}

func (a *Access) beginTeardown(ctx context.Context, resource *sandboxapi.Sandbox, name, uid string) error {
	if resource.UID != types.UID(uid) {
		return &SandboxNotFoundError{Name: name}
	}
	if resource.DeletionTimestamp != nil {
		return nil
	}
	switch resource.Status.OperatingMode {
	case sandboxapi.OperatingModeRunning:
		return &SandboxRunningError{Name: name}
	case sandboxapi.OperatingModeSuspended:
		uidPrecondition := resource.UID
		resourceVersion := resource.ResourceVersion
		err := a.objects.Delete(ctx, resource, &client.Preconditions{
			UID: &uidPrecondition, ResourceVersion: &resourceVersion,
		})
		if err != nil {
			return err
		}
		return nil
	default:
		return &SandboxModeUnknownError{Name: name}
	}
}

func (a *Access) retryConflict(operation func() error) error {
	for attempt := 0; attempt < mutationAttempts; attempt++ {
		err := operation()
		if err == nil || !apierrors.IsConflict(err) || attempt == mutationAttempts-1 {
			return err
		}
	}
	return nil
}

func (a *Access) requirePolicySets(ctx context.Context, names []string) error {
	var sets policyapi.PolicySetList
	if err := a.objects.List(ctx, &sets, client.InNamespace(a.namespace)); err != nil {
		return fmt.Errorf("list PolicySets: %w", err)
	}
	known := make(map[string]struct{}, len(sets.Items))
	for _, set := range sets.Items {
		known[set.Name] = struct{}{}
	}
	missing := make([]string, 0)
	for _, name := range names {
		if _, ok := known[name]; !ok {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		return &UnknownPolicySetError{Names: missing, Kind: "PolicySet"}
	}
	return nil
}

func (a *Access) retry(ctx context.Context, name, intent string) (*sandboxapi.Sandbox, error) {
	resource, err := a.read(ctx, name)
	if err != nil {
		var missing *SandboxNotFoundError
		if errors.As(err, &missing) {
			return nil, nil
		}
		return nil, err
	}
	if resource.DeletionTimestamp != nil || resource.Annotations[CreateIntentAnnotation] != intent {
		return nil, &SandboxConflictError{Name: name}
	}
	return resource, nil
}

func (a *Access) incarnation(ctx context.Context, name, uid string) (*sandboxapi.Sandbox, error) {
	resource, err := a.read(ctx, name)
	if err != nil {
		return nil, err
	}
	if string(resource.UID) != uid {
		return nil, &SandboxNotFoundError{Name: name}
	}
	return resource, nil
}

func (a *Access) read(ctx context.Context, name string) (*sandboxapi.Sandbox, error) {
	view, err := a.reader.Get(ctx, name)
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, &SandboxNotFoundError{Name: name}
		}
		return nil, err
	}
	return view.Resource, nil
}

func (a *Access) patchSpec(ctx context.Context, resource *sandboxapi.Sandbox, spec map[string]any) error {
	patch, err := json.Marshal(map[string]any{
		"metadata": map[string]any{"uid": string(resource.UID), "resourceVersion": resource.ResourceVersion},
		"spec":     spec,
	})
	if err != nil {
		return fmt.Errorf("marshal Sandbox patch: %w", err)
	}
	return a.objects.Patch(ctx, resource, client.RawPatch(types.MergePatchType, patch))
}

func sandboxForCreate(
	request *sandboxespb.CreateSandboxRequest,
	namespace string,
	policySets []string,
	intent string,
) *sandboxapi.Sandbox {
	annotations := map[string]string{CreateIntentAnnotation: intent}
	labels := map[string]string{}
	if request.Preset != nil {
		labels[PresetLabel] = request.GetPreset()
	}
	inboxTags := uniqueSorted(request.GetInboxTags())
	resource := &sandboxapi.Sandbox{
		TypeMeta: metav1.TypeMeta{APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind},
		ObjectMeta: metav1.ObjectMeta{
			Namespace: namespace, Name: request.GetName(), Annotations: annotations,
		},
		Spec: sandboxapi.SandboxSpec{
			Template: request.GetTemplate(), Running: true, PolicySets: append([]string(nil), policySets...),
			WakeOnSend: request.GetWakeOnSend(), InboxTags: inboxTags, Bootstrap: request.GetBootstrap(),
		},
	}
	if len(labels) > 0 {
		resource.Labels = labels
	}
	if request.IdleSuspendAfter != nil {
		resource.Spec.IdleSuspendAfter = &metav1.Duration{Duration: request.IdleSuspendAfter.AsDuration()}
	}
	return resource
}

func validateCreate(request *sandboxespb.CreateSandboxRequest) error {
	if request == nil {
		return &InvalidSandboxError{Reason: "valid name and template are required"}
	}
	name := request.GetName()
	if !validSandboxName(name) || len(name) > 57 || request.GetTemplate() == "" {
		return &InvalidSandboxError{Reason: "valid name and template are required"}
	}
	if request.Preset != nil && !validLabelValue(request.GetPreset()) {
		return &InvalidSandboxError{Reason: "preset must be a valid label value"}
	}
	if utf8.RuneCountInString(request.GetBootstrap()) > maxBootstrapCharacters {
		return &InvalidSandboxError{Reason: "script exceeds 65536 characters"}
	}
	if request.IdleSuspendAfter != nil {
		if err := request.IdleSuspendAfter.CheckValid(); err != nil || request.IdleSuspendAfter.AsDuration() < 0 {
			return &InvalidSandboxError{Reason: "idle suspend duration is invalid"}
		}
	}
	return nil
}

func validSandboxName(value string) bool {
	if len(value) == 0 || len(value) > 57 {
		return false
	}
	if !isLowerAlphaNumeric(value[0]) || !isLowerAlphaNumeric(value[len(value)-1]) {
		return false
	}
	for _, character := range value {
		if character > 0x7f {
			return false
		}
		if !isLowerAlphaNumeric(byte(character)) && character != '-' {
			return false
		}
	}
	return true
}

func validLabelValue(value string) bool {
	if value == "" || len(value) > 63 {
		return false
	}
	if !isAlphaNumeric(value[0]) || !isAlphaNumeric(value[len(value)-1]) {
		return false
	}
	for _, character := range value {
		if character > 0x7f {
			return false
		}
		if !isAlphaNumeric(byte(character)) && character != '-' && character != '_' && character != '.' {
			return false
		}
	}
	return true
}

func isLowerAlphaNumeric(value byte) bool {
	return value >= 'a' && value <= 'z' || value >= '0' && value <= '9'
}

func isAlphaNumeric(value byte) bool {
	return value >= 'a' && value <= 'z' || value >= 'A' && value <= 'Z' || value >= '0' && value <= '9'
}

func createIntent(request *sandboxespb.CreateSandboxRequest, caller *sandboxespb.ServiceAccount) (string, error) {
	requestJSON, err := (protojson.MarshalOptions{UseProtoNames: true}).Marshal(request)
	if err != nil {
		return "", err
	}
	var requestValue any
	if err := json.Unmarshal(requestJSON, &requestValue); err != nil {
		return "", err
	}
	value := map[string]any{
		"caller":  map[string]any{"name": caller.GetName(), "namespace": caller.GetNamespace()},
		"request": requestValue,
	}
	return pythonJSON(value)
}

func pythonJSON(value any) (string, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	// Python's json.dumps(sort_keys=True, separators=(",", ":")) sorts map keys and escapes
	// non-ASCII characters. Go also escapes HTML by default, while Python does not.
	var raw any
	if err := json.Unmarshal(encoded, &raw); err != nil {
		return "", err
	}
	var buffer strings.Builder
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(raw); err != nil {
		return "", err
	}
	canonical := strings.TrimSuffix(buffer.String(), "\n")
	var ascii strings.Builder
	for _, character := range canonical {
		if character <= 0x7f {
			ascii.WriteRune(character)
			continue
		}
		if character <= 0xffff {
			fmt.Fprintf(&ascii, `\u%04x`, character)
			continue
		}
		character -= 0x10000
		high := rune(0xd800 + (character >> 10))
		low := rune(0xdc00 + (character & 0x3ff))
		fmt.Fprintf(&ascii, `\u%04x\u%04x`, high, low)
	}
	return ascii.String(), nil
}

func sortedKeys(set map[string]struct{}) []string {
	keys := make([]string, 0, len(set))
	for key := range set {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func uniqueSorted(values []string) []string {
	set := make(map[string]struct{}, len(values))
	for _, value := range values {
		set[value] = struct{}{}
	}
	return sortedKeys(set)
}

func sameDuration(first, second *metav1.Duration) bool {
	if first == nil || second == nil {
		return first == nil && second == nil
	}
	return first.Duration == second.Duration
}

type (
	corev1Pod     = corev1.Pod
	corev1PodList = corev1.PodList
)
