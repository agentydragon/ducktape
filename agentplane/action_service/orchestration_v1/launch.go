package orchestrationv1

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

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
func (e *Executor) launch(ctx context.Context, caller store.ServiceAccount, args contract.LaunchArgs) (Launched, error) {
	for _, list := range [][]string{args.PolicySets, args.InboxTags, args.LauncherGrants} {
		slices.Sort(list)
	}
	request := &environmentpb.CreateEnvironmentRequest{
		Name: args.Name, Template: args.Template, PolicySets: args.PolicySets, InboxTags: args.InboxTags,
		Bootstrap: args.Bootstrap, WakeOnSend: args.WakeOnSend,
		LaunchOrigin: &environmentpb.EnvironmentLaunchOrigin{
			Caller:      &identitypb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name},
			ActionGroup: e.group, LauncherGrants: args.LauncherGrants,
		},
	}
	if args.IdleSuspendAfterSeconds != nil {
		request.IdleSuspendAfter = durationpb.New(time.Duration(*args.IdleSuspendAfterSeconds) * time.Second)
	}
	if e.environment == nil {
		return Launched{}, fmt.Errorf("%w: EnvironmentService is unavailable", worker.ErrOutcomeUnknown)
	}
	target, err := e.environment.CreateEnvironment(ctx, request)
	if err != nil {
		return Launched{}, launchError(err)
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

func launchError(err error) error {
	result := status.Convert(err)
	if result.Code() == codes.AlreadyExists {
		return refuse("sandbox_exists", "%s", result.Message())
	}
	if result.Code() == codes.InvalidArgument {
		for _, detail := range result.Details() {
			if info, ok := detail.(*errdetails.ErrorInfo); ok && info.Domain == "agentplane.environments" {
				switch info.Reason {
				case "UNKNOWN_POLICY_SET":
					return refuse("unknown_policy_set", "%s", result.Message())
				case "LAUNCHER_POLICY_SET":
					return refuse("launcher_policy_set", "%s", result.Message())
				}
			}
		}
		return refuse("invalid_arguments", "%s", result.Message())
	}
	if result.Code() == codes.PermissionDenied || result.Code() == codes.Unauthenticated {
		return refuse("environment_refused", "%s", result.Message())
	}
	return fmt.Errorf("%w: EnvironmentService create: %w", worker.ErrOutcomeUnknown, err)
}

// grant writes the launcher grants on sandbox for caller, when there are any: a PolicySet of their rules and a
// PolicyBinding of it to caller. Both are owned by the Sandbox, so deleting it revokes them.
func (e *Executor) grant(ctx context.Context, caller store.ServiceAccount, sandbox *sandboxapi.Sandbox, grants []string) error {
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
	return e.createOwned(ctx, &policyapi.PolicyBinding{ObjectMeta: *meta.DeepCopy(), Spec: policyapi.PolicyBindingSpec{
		Subject:    policyapi.Subject{Namespace: caller.Namespace, Name: caller.Name},
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
		return refuse("launcher_grant_exists", "%s exists and is not this Sandbox's", object.GetName())
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
			return refuse("kubernetes_refused", "%s: %s", doing, failed.Status().Message)
		}
	}
	return fmt.Errorf("%w: %s: %w", worker.ErrOutcomeUnknown, doing, err)
}
