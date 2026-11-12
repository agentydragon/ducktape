// Package management implements the EnvironmentService's Kubernetes mutations.
package management

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"sort"
	"time"
	"unicode/utf8"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/internal/view"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const maxBootstrapCharacters = 65_536

// Access scopes Sandbox operations to one namespace, caller identity, and set of default policies.
type Access struct {
	namespace string
	objects   client.Client
	reader    *sandboxreader.Reader
	caller    *identitypb.ServiceAccount
}

// New creates access over one Sandbox namespace and caller identity.
func New(
	namespace string,
	objects client.Client,
	caller *identitypb.ServiceAccount,
) (*Access, error) {
	if namespace == "" || objects == nil || caller == nil || caller.Namespace == "" || caller.Name == "" {
		return nil, errors.New("Sandbox namespace, Kubernetes client, and caller ServiceAccount are required")
	}
	reader, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	return &Access{
		namespace: namespace, objects: objects, reader: reader,
		caller: &identitypb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name},
	}, nil
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

// Create validates and creates a running Sandbox, applying the process defaults and recording an
// immutable caller/request intent so an identical retry is idempotent.
func (a *Access) Create(ctx context.Context, request *environmentpb.CreateEnvironmentRequest) (*identitypb.EnvironmentTarget, error) {
	if err := validateCreate(request); err != nil {
		return nil, err
	}
	intent, err := encodeCreateIntent(request, a.caller)
	if err != nil {
		return nil, &InvalidSandboxError{Reason: "create request cannot be recorded"}
	}
	if existing, err := a.retry(ctx, request); err != nil {
		return nil, err
	} else if existing != nil {
		return &identitypb.EnvironmentTarget{Namespace: existing.Namespace, Name: existing.Name, Uid: string(existing.UID)}, nil
	}

	if err := a.requireIdentity(ctx, request.GetIdentity()); err != nil {
		return nil, err
	}

	resource := sandboxForCreate(request, a.namespace, intent)
	if err := a.objects.Create(ctx, resource); err != nil {
		existing, retryErr := a.retry(ctx, request)
		if retryErr != nil {
			return nil, retryErr
		}
		if existing == nil {
			return nil, err
		}
		resource = existing
	}
	return &identitypb.EnvironmentTarget{Namespace: resource.Namespace, Name: resource.Name, Uid: string(resource.UID)}, nil
}

// Suspend requests that the exact current Sandbox stop. It does not wait for controller status.
func (a *Access) Suspend(ctx context.Context, name, uid string) error {
	if uid == "" {
		return &InvalidSandboxError{Reason: "Sandbox UID is required"}
	}
	return a.setStopped(ctx, name, uid)
}

// SetWakeOnSend changes the durable wake policy on the UID selected by the caller.
func (a *Access) SetWakeOnSend(ctx context.Context, name, uid string, wakeOnSend bool) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
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
		if err := validateIdleSuspendAfter(value); err != nil {
			return err
		}
		duration = &metav1.Duration{Duration: value.AsDuration()}
	}
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
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

// ResolveIdentity pins the name chosen by the operator to its current incarnation for the
// Dispatcher guard. A change between this read and the patch is refused by SetIdentity.
func (a *Access) ResolveIdentity(ctx context.Context, name string) (*identitypb.Identity, error) {
	if name == "" {
		return nil, nil
	}
	identity := &policyapi.Identity{}
	err := a.objects.Get(ctx, types.NamespacedName{Namespace: a.namespace, Name: name}, identity)
	if client.IgnoreNotFound(err) != nil {
		return nil, fmt.Errorf("read Identity %s: %w", name, err)
	}
	if err != nil || identity.UID == "" || identity.DeletionTimestamp != nil {
		return nil, &InvalidSandboxError{Reason: fmt.Sprintf("Identity %s is unavailable", name)}
	}
	return &identitypb.Identity{Name: name, Uid: string(identity.UID)}, nil
}

// SetIdentity patches only spec.identity on the selected stopped Sandbox. The caller holds the
// Dispatcher's exclusive per-Sandbox admission lock until this patch returns.
func (a *Access) SetIdentity(ctx context.Context, name, uid string, expected *identitypb.Identity) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		resource, err := a.incarnation(ctx, name, uid)
		if err != nil {
			return err
		}
		if resource.DeletionTimestamp != nil {
			return &SandboxTearingDownError{Name: name}
		}
		if resource.Status.OperatingMode != "" && resource.Status.StopStage != sandboxapi.StopStageStopped {
			return &IdentityChangeRunningError{Name: name}
		}
		if expected != nil {
			current, err := a.ResolveIdentity(ctx, expected.GetName())
			if err != nil {
				return err
			}
			if current.GetUid() != expected.GetUid() {
				return &SandboxConflictError{Name: "Identity changed; refresh and try again"}
			}
		}
		if expected == nil {
			if resource.Spec.Identity == nil {
				return nil
			}
			return a.patchSpec(ctx, resource, map[string]any{"identity": nil})
		}
		if resource.Spec.Identity != nil && resource.Spec.Identity.Name == expected.GetName() {
			return nil
		}
		return a.patchSpec(ctx, resource, map[string]any{"identity": map[string]string{"name": expected.GetName()}})
	})
}

// Delete begins teardown of the exact current Sandbox. Running Sandboxes are refused; repeated
// requests against a Sandbox already deleting are idempotent.
func (a *Access) Delete(ctx context.Context, name, uid string) error {
	if uid == "" {
		return &InvalidSandboxError{Reason: "Sandbox UID is required"}
	}
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		resource, err := a.incarnation(ctx, name, uid)
		if err != nil {
			return err
		}
		return a.beginTeardown(ctx, resource, name, uid)
	})
}

func (a *Access) setStopped(ctx context.Context, name, uid string) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		resource, err := a.incarnation(ctx, name, uid)
		if err != nil {
			return err
		}
		if resource.DeletionTimestamp != nil {
			return &SandboxTearingDownError{Name: name}
		}
		if !resource.Spec.Running {
			return nil
		}
		return a.patchSpec(ctx, resource, map[string]any{"running": false})
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

// requireIdentity checks that the Identity a new Sandbox will act as exists and is not being deleted.
func (a *Access) requireIdentity(ctx context.Context, name string) error {
	identity := &policyapi.Identity{}
	if err := a.objects.Get(ctx, client.ObjectKey{Namespace: a.namespace, Name: name}, identity); err != nil {
		if apierrors.IsNotFound(err) {
			return &InvalidSandboxError{Reason: fmt.Sprintf("Identity %s does not exist", name)}
		}
		return fmt.Errorf("read Identity: %w", err)
	}
	if identity.DeletionTimestamp != nil {
		return &InvalidSandboxError{Reason: fmt.Sprintf("Identity %s is being deleted", name)}
	}
	return nil
}

// retry returns the Sandbox an earlier identical create by this caller made, nil when the name is free, and a
// conflict when the name is taken by anything else.
func (a *Access) retry(ctx context.Context, request *environmentpb.CreateEnvironmentRequest) (*sandboxapi.Sandbox, error) {
	name := request.GetName()
	resource, err := a.read(ctx, name)
	if err != nil {
		var missing *SandboxNotFoundError
		if errors.As(err, &missing) {
			return nil, nil
		}
		return nil, err
	}
	if resource.DeletionTimestamp != nil || !sameIntent(resource.Annotations[sandboxapi.CreateIntentAnnotation], request, a.caller) {
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
	request *environmentpb.CreateEnvironmentRequest,
	namespace string,
	intent string,
) *sandboxapi.Sandbox {
	annotations := map[string]string{sandboxapi.CreateIntentAnnotation: intent}
	labels := map[string]string{}
	if request.Preset != nil {
		labels[view.PresetLabel] = request.GetPreset()
	}
	inboxTags := uniqueSorted(request.GetInboxTags())
	resource := &sandboxapi.Sandbox{
		TypeMeta: metav1.TypeMeta{APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind},
		ObjectMeta: metav1.ObjectMeta{
			Namespace: namespace, Name: request.GetName(), Annotations: annotations,
		},
		Spec: sandboxapi.SandboxSpec{
			Template: request.GetTemplate(), Running: true, Identity: &sandboxapi.IdentityRef{Name: request.GetIdentity()},
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

func validateCreate(request *environmentpb.CreateEnvironmentRequest) error {
	if request == nil {
		return &InvalidSandboxError{Reason: "valid name and template are required"}
	}
	name := request.GetName()
	if !validSandboxName(name) || len(name) > 57 || request.GetTemplate() == "" {
		return &InvalidSandboxError{Reason: "valid name and template are required"}
	}
	if request.GetIdentity() == "" {
		return &InvalidSandboxError{Reason: "the Identity the Environment acts as is required"}
	}
	if request.Preset != nil && !validLabelValue(request.GetPreset()) {
		return &InvalidSandboxError{Reason: "preset must be a valid label value"}
	}
	if utf8.RuneCountInString(request.GetBootstrap()) > maxBootstrapCharacters {
		return &InvalidSandboxError{Reason: "script exceeds 65536 characters"}
	}
	if request.IdleSuspendAfter != nil {
		if err := validateIdleSuspendAfter(request.IdleSuspendAfter); err != nil {
			return err
		}
	}
	return nil
}

// Reject protobuf durations that saturate when converted to Go's narrower duration range.
func validateIdleSuspendAfter(value *durationpb.Duration) error {
	if err := value.CheckValid(); err != nil || value.AsDuration() < time.Minute || !proto.Equal(value, durationpb.New(value.AsDuration())) {
		return &InvalidSandboxError{Reason: "idle suspend duration must be at least one minute and representable"}
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

// createIntent is the receipt a create records on its Sandbox: the caller and the request it made.
// The encoding needs only to be decodable; retries are matched by sameIntent, never by its bytes.
type createIntent struct {
	Caller  intentCaller    `json:"caller"`
	Request json.RawMessage `json:"request"`
}

type intentCaller struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

func encodeCreateIntent(request *environmentpb.CreateEnvironmentRequest, caller *identitypb.ServiceAccount) (string, error) {
	// Proto field names, as receipts have always spelled them and readers of the annotation expect.
	requestJSON, err := (protojson.MarshalOptions{UseProtoNames: true}).Marshal(request)
	if err != nil {
		return "", err
	}
	encoded, err := json.Marshal(createIntent{
		Caller:  intentCaller{Namespace: caller.GetNamespace(), Name: caller.GetName()},
		Request: requestJSON,
	})
	if err != nil {
		return "", err
	}
	return string(encoded), nil
}

// sameIntent reports whether recorded is a create by caller of request. A receipt that does not decode is
// someone else's.
func sameIntent(recorded string, request *environmentpb.CreateEnvironmentRequest, caller *identitypb.ServiceAccount) bool {
	var intent createIntent
	if err := json.Unmarshal([]byte(recorded), &intent); err != nil {
		return false
	}
	if intent.Caller.Namespace != caller.GetNamespace() || intent.Caller.Name != caller.GetName() {
		return false
	}
	var recordedRequest environmentpb.CreateEnvironmentRequest
	if err := protojson.Unmarshal(intent.Request, &recordedRequest); err != nil {
		return false
	}
	return proto.Equal(&recordedRequest, request)
}

func uniqueSorted(values []string) []string {
	values = append([]string{}, values...)
	slices.Sort(values)
	return slices.Compact(values)
}

func sameDuration(first, second *metav1.Duration) bool {
	if first == nil || second == nil {
		return first == nil && second == nil
	}
	return first.Duration == second.Duration
}
