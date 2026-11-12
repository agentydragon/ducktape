package orchestrationv1

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// inboxAccessRule is the Notification Service's rule type for sends to Session inboxes
// (notification_service/inboxaccess).
const inboxAccessRule = "session_inbox_access"

// Launched is the result of launch_sandbox.
type Launched struct {
	Sandbox contract.SandboxRef `json:"sandbox"`
	// LauncherGrants are what its launcher may do to it without asking again.
	LauncherGrants []string `json:"launcher_grants"`
}

// launcherName names the PolicySet and PolicyBinding of a Sandbox's launcher grants. A Sandbox's name has no dot, so it
// never names the PolicyBinding the Sandbox Controller writes for a Sandbox's own account, which takes the Sandbox's
// name.
func launcherName(sandbox string) string { return sandbox + ".launcher" }

// launch creates the Environment through its owning service, then writes Action-owned launcher grants.
// The authorized Action broker supplies caller and grant intent so same-name retries cannot adopt
// another launch, including one that requested a different set of grants.
func (e *Executor) launch(ctx context.Context, caller store.Caller, args contract.LaunchArgs) (Launched, error) {
	for _, list := range [][]string{args.PolicySets, args.InboxTags, args.LauncherGrants} {
		slices.Sort(list)
	}
	if e.environment == nil {
		return Launched{}, fmt.Errorf("%w: EnvironmentService is unavailable", worker.ErrOutcomeUnknown)
	}
	origin, launcher, err := e.launchCaller(ctx, caller)
	if err != nil {
		return Launched{}, err
	}
	origin.ActionGroup, origin.LauncherGrants = e.group, args.LauncherGrants
	// A launch is three operations: the Identity the Sandbox acts as, its PolicySets bound to that
	// Identity, then the Sandbox naming it. No environment operation creates an Identity.
	identity, created, err := e.launchIdentity(ctx, launcher, args)
	if err != nil {
		return Launched{}, err
	}
	request := &environmentpb.CreateEnvironmentRequest{
		Name: args.Name, Template: args.Template, Identity: identity.Name, InboxTags: args.InboxTags,
		Bootstrap: args.Bootstrap, WakeOnSend: args.WakeOnSend,
		LaunchOrigin: origin,
	}
	if args.IdleSuspendAfterSeconds != nil {
		request.IdleSuspendAfter = durationpb.New(time.Duration(*args.IdleSuspendAfterSeconds) * time.Second)
	}
	target, err := e.environment.CreateEnvironment(ctx, request)
	if err != nil {
		failure := launchError(err)
		var refused *refusal
		if created && errors.As(failure, &refused) {
			// Roll back only an Identity this attempt created after a definite refusal.
			e.deleteLaunchIdentity(ctx, identity)
		}
		return Launched{}, failure
	}
	if target.GetNamespace() != e.binding.Namespace || target.GetName() != args.Name || target.GetUid() == "" {
		return Launched{}, fmt.Errorf("%w: EnvironmentService returned an invalid created target", worker.ErrOutcomeUnknown)
	}
	var sandbox sandboxapi.Sandbox
	if err := e.kube.Get(ctx, client.ObjectKey{Namespace: target.GetNamespace(), Name: target.GetName()}, &sandbox); err != nil {
		return Launched{}, fmt.Errorf("%w: reading created Environment: %w", worker.ErrOutcomeUnknown, err)
	}
	if string(sandbox.UID) != target.GetUid() || sandbox.DeletionTimestamp != nil {
		return Launched{}, fmt.Errorf("%w: created Environment was replaced or deleted before launcher grants", worker.ErrOutcomeUnknown)
	}
	if err := e.grant(ctx, caller, &sandbox, args.LauncherGrants); err != nil {
		var refused *refusal
		if errors.As(err, &refused) {
			refused.message = fmt.Sprintf("Sandbox %q was launched, but its launcher grants were not written: %s", sandbox.Name, refused.message)
		}
		return Launched{}, err
	}
	return Launched{Sandbox: contract.SandboxRef{Name: sandbox.Name, UID: sandbox.UID}, LauncherGrants: append([]string{}, args.LauncherGrants...)}, nil
}

// launchCaller preserves the authenticated effective caller separately from the Action Service actor.
func (e *Executor) launchCaller(ctx context.Context, caller store.Caller) (*environmentpb.EnvironmentLaunchOrigin, string, error) {
	switch caller := caller.(type) {
	case store.ServiceAccountCaller:
		if caller.Account.Namespace != "" && caller.Account.Name != "" {
			return &environmentpb.EnvironmentLaunchOrigin{Caller: &identitypb.ServiceAccount{Namespace: caller.Account.Namespace, Name: caller.Account.Name}}, "serviceaccount:" + caller.Account.Namespace + "/" + caller.Account.Name, nil
		}
	case store.IdentityCaller:
		var identity policyapi.Identity
		if err := e.kube.Get(ctx, client.ObjectKey{Namespace: e.binding.Namespace, Name: caller.Identity.Name}, &identity); err != nil {
			return nil, "", kubeError("reading launcher Identity", err)
		}
		if caller.Identity.UID != "" && string(identity.UID) == caller.Identity.UID && identity.DeletionTimestamp == nil {
			return &environmentpb.EnvironmentLaunchOrigin{CallerIdentity: &identitypb.Identity{Name: caller.Identity.Name, Uid: caller.Identity.UID}}, "identity:" + caller.Identity.Name + "/" + caller.Identity.UID, nil
		}
	}
	return nil, "", refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_ENVIRONMENT_REFUSED, "launch caller is unknown or its Identity was replaced")
}

// launchIdentityAnnotation and launchSetsAnnotation record which caller's launch created an Identity
// and what it granted, so a retry of the same launch adopts it while another caller's launch, another
// launch of the name or a foreign Identity of the name is refused.
const (
	launchIdentityAnnotation = "agentplane.allegedly.works/launched-by"
	launchSetsAnnotation     = "agentplane.allegedly.works/launch-policy-sets"
)

// launchIdentity creates, or on a retry adopts, the Identity a launched Sandbox acts as, named after
// it, and binds the request's PolicySets to it in one binding the Identity owns.
func (e *Executor) launchIdentity(ctx context.Context, launcher string, args contract.LaunchArgs) (*policyapi.Identity, bool, error) {
	sets := slices.Compact(slices.Sorted(slices.Values(args.PolicySets)))
	if err := e.requireBindablePolicySets(ctx, sets); err != nil {
		return nil, false, err
	}
	intent := strings.Join(sets, ",")
	identity := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{
			Name: args.Name, Namespace: e.binding.Namespace,
			Annotations: map[string]string{launchIdentityAnnotation: launcher, launchSetsAnnotation: intent},
		},
		Spec: policyapi.IdentitySpec{Description: fmt.Sprintf("Agent of Sandbox %s, launched by %s through %s.", args.Name, launcher, e.group)},
	}
	created := true
	if err := e.kube.Create(ctx, identity); apierrors.IsAlreadyExists(err) {
		created = false
		if err := e.kube.Get(ctx, client.ObjectKeyFromObject(identity), identity); err != nil {
			return nil, false, kubeError("reading Identity "+args.Name, err)
		}
		if identity.Annotations[launchIdentityAnnotation] != launcher || identity.Annotations[launchSetsAnnotation] != intent || identity.DeletionTimestamp != nil {
			return nil, false, refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_EXISTS, "Identity %s exists and is not this launch's", args.Name)
		}
	} else if err != nil {
		return nil, false, kubeError("creating Identity "+args.Name, err)
	}
	if len(sets) == 0 {
		return identity, created, nil
	}
	controller, blockOwnerDeletion := false, false
	binding := &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: args.Name + ".identity", Namespace: e.binding.Namespace,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "Identity", Name: identity.Name, UID: identity.UID,
				Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
			}},
		},
		Spec: policyapi.PolicyBindingSpec{Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: identity.Name}}, PolicySets: sets},
	}
	if err := e.createOwned(ctx, binding, identity.UID); err != nil {
		return nil, false, err
	}
	return identity, created, nil
}

// requireBindablePolicySets refuses a launch naming a PolicySet that does not exist, or another
// Sandbox's launcher grant: bound to the new Identity, it would give it the first launcher's grant.
func (e *Executor) requireBindablePolicySets(ctx context.Context, names []string) error {
	var list policyapi.PolicySetList
	if err := e.kube.List(ctx, &list, client.InNamespace(e.binding.Namespace)); err != nil {
		return kubeError("listing PolicySets", err)
	}
	known := map[string]bool{}
	for _, set := range list.Items {
		known[set.Name] = strings.HasSuffix(set.Name, ".launcher") || slices.ContainsFunc(set.OwnerReferences, func(owner metav1.OwnerReference) bool {
			return owner.Kind == sandboxapi.SandboxKind
		})
	}
	var missing, launcher []string
	for _, name := range names {
		if reserved, ok := known[name]; !ok {
			missing = append(missing, name)
		} else if reserved {
			launcher = append(launcher, name)
		}
	}
	if len(missing) > 0 {
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNKNOWN_POLICY_SET, "unknown PolicySets: %s", strings.Join(missing, ", "))
	}
	if len(launcher) > 0 {
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_LAUNCHER_POLICY_SET, "PolicySets %s are other Sandboxes' launcher grants", strings.Join(launcher, ", "))
	}
	return nil
}

// deleteLaunchIdentity deletes exactly the Identity this launch created, and by garbage collection
// its binding, after the Sandbox was refused. A failure only leaves an unreferenced Identity behind.
func (e *Executor) deleteLaunchIdentity(ctx context.Context, identity *policyapi.Identity) {
	cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), e.readBudget)
	defer cancel()
	var sandboxes sandboxapi.SandboxList
	if err := e.kube.List(cleanup, &sandboxes, client.InNamespace(identity.Namespace)); err != nil {
		slog.WarnContext(ctx, "orchestration_v1 could not check a refused launch's Identity references", "identity", identity.Name, "error", err)
		return
	}
	// Another launch can adopt this attempt's Identity and win creation before this refusal.
	if slices.ContainsFunc(sandboxes.Items, func(sandbox sandboxapi.Sandbox) bool {
		return sandbox.Spec.Identity != nil && sandbox.Spec.Identity.Name == identity.Name
	}) {
		return
	}
	uid, resourceVersion := identity.UID, identity.ResourceVersion
	if err := e.kube.Delete(cleanup, identity, client.Preconditions{UID: &uid, ResourceVersion: &resourceVersion}); err != nil && !apierrors.IsNotFound(err) {
		slog.WarnContext(ctx, "orchestration_v1 could not delete a refused launch's Identity", "identity", identity.Name, "error", err)
	}
}

func launchError(err error) error {
	result := status.Convert(err)
	if result.Code() == codes.AlreadyExists {
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_EXISTS, "%s", result.Message())
	}
	if result.Code() == codes.InvalidArgument {
		for _, detail := range result.Details() {
			if info, ok := detail.(*errdetails.ErrorInfo); ok && info.Domain == "agentplane.environments" {
				switch info.Reason {
				case "UNKNOWN_POLICY_SET":
					return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNKNOWN_POLICY_SET, "%s", result.Message())
				case "LAUNCHER_POLICY_SET":
					return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_LAUNCHER_POLICY_SET, "%s", result.Message())
				}
			}
		}
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INVALID_ARGUMENTS, "%s", result.Message())
	}
	if result.Code() == codes.PermissionDenied || result.Code() == codes.Unauthenticated {
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_ENVIRONMENT_REFUSED, "%s", result.Message())
	}
	return fmt.Errorf("%w: EnvironmentService create: %w", worker.ErrOutcomeUnknown, err)
}

// grant writes the launcher grants on sandbox for caller, when there are any: a PolicySet of their rules and a
// PolicyBinding of it to caller. The Sandbox owns both; an Identity caller also owns its binding,
// which pins that incarnation while either owner remains.
func (e *Executor) grant(ctx context.Context, caller store.Caller, sandbox *sandboxapi.Sandbox, grants []string) error {
	if len(grants) == 0 {
		return nil
	}
	rules, err := e.launcherRules(contract.SandboxRef{Name: sandbox.Name, UID: sandbox.UID}, grants)
	if err != nil {
		return err
	}
	meta := metav1.ObjectMeta{
		Name: launcherName(sandbox.Name), Namespace: e.binding.Namespace,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: sandbox.Name, UID: sandbox.UID,
		}},
	}
	if err := e.createOwned(ctx, &policyapi.PolicySet{ObjectMeta: meta, Spec: policyapi.PolicySetSpec{Rules: rules}}, sandbox.UID); err != nil {
		return err
	}
	bindingMeta := *meta.DeepCopy()
	var subject policyapi.Subject
	switch caller := caller.(type) {
	case store.ServiceAccountCaller:
		subject = policyapi.Subject{Namespace: caller.Account.Namespace, Name: caller.Account.Name}
	case store.IdentityCaller:
		subject = policyapi.Subject{Identity: &policyapi.IdentityRef{Name: caller.Identity.Name}}
		bindingMeta.OwnerReferences = append(bindingMeta.OwnerReferences, metav1.OwnerReference{
			APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "Identity", Name: caller.Identity.Name, UID: types.UID(caller.Identity.UID),
		})
	default:
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_ENVIRONMENT_REFUSED, "launch caller is unknown")
	}
	return e.createOwned(ctx, &policyapi.PolicyBinding{ObjectMeta: bindingMeta, Spec: policyapi.PolicyBindingSpec{
		Subject:    subject,
		PolicySets: []string{meta.Name},
	}}, sandbox.UID)
}

// launcherRules are the rules granting what grants names on sandbox: send_message as the Notification Service's inbox
// rule, and this group's Actions as an auto-approval of the requests whose `sandbox` argument is that incarnation.
func (e *Executor) launcherRules(sandbox contract.SandboxRef, grants []string) ([]policyapi.Rule, error) {
	var rules []policyapi.Rule
	actions := slices.DeleteFunc(slices.Clone(grants), func(grant string) bool { return grant == contract.GrantSendMessage })
	if len(actions) < len(grants) {
		rules = append(rules, policyapi.Rule{
			Type: inboxAccessRule, Operations: []string{contract.GrantSendMessage},
			Targets: &policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{{Name: sandbox.Name, UID: sandbox.UID}}},
		})
	}
	if len(actions) > 0 {
		schema, err := json.Marshal(map[string]any{
			"type": "object", "required": []string{"sandbox"},
			"properties": map[string]any{"sandbox": map[string]any{"const": sandbox}},
		})
		if err != nil {
			return nil, err
		}
		rules = append(rules, policyapi.Rule{Type: policies.RuleType, Policy: &policyapi.ActionPolicy{
			Type: string(store.PolicyArgumentSchema), Actions: map[string]policyapi.ActionNames{e.group: actions},
			Schema: &runtime.RawExtension{Raw: schema},
		}})
	}
	return rules, nil
}

// createOwned creates object, or accepts the one an earlier attempt at the launch created: one the same Sandbox owns.
func (e *Executor) createOwned(ctx context.Context, object client.Object, owner types.UID) error {
	err := e.kube.Create(ctx, object)
	if !apierrors.IsAlreadyExists(err) {
		if err != nil {
			return kubeError("creating "+object.GetName(), err)
		}
		return nil
	}
	existing := object.DeepCopyObject().(client.Object)
	if err := e.kube.Get(ctx, client.ObjectKeyFromObject(object), existing); err != nil {
		return kubeError("reading "+object.GetName(), err)
	}
	if !slices.ContainsFunc(existing.GetOwnerReferences(), func(reference metav1.OwnerReference) bool {
		return reference.UID == owner
	}) {
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_LAUNCHER_GRANT_EXISTS, "%s exists and is not this Sandbox's", object.GetName())
	}
	return nil
}

// kubeError is err from the API server as the attempt's outcome. A request the API server refused had no effect, and
// is a refusal; any other failure leaves the attempt's outcome unknown, even a read's, as an attempt that has written
// can stop at a later read.
func kubeError(doing string, err error) error {
	var failed apierrors.APIStatus
	if errors.As(err, &failed) {
		if code := failed.Status().Code; code >= 400 && code < 500 && code != http.StatusRequestTimeout && code != http.StatusTooManyRequests {
			return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_KUBERNETES_REFUSED, "%s: %s", doing, failed.Status().Message)
		}
	}
	return fmt.Errorf("%w: %s: %w", worker.ErrOutcomeUnknown, doing, err)
}
