package orchestrationv1

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// inboxAccessRule is the Notification Service's rule type for sends to Session inboxes
// (notification_service/inboxaccess).
const inboxAccessRule = "session_inbox_access"

type launchArgs struct {
	Name       string   `json:"name"`
	Template   string   `json:"template"`
	PolicySets []string `json:"policy_sets,omitempty"`
	InboxTags  []string `json:"inbox_tags,omitempty"`
	Bootstrap  string   `json:"bootstrap,omitempty"`
	WakeOnSend bool     `json:"wake_on_send,omitempty"`
	// IdleSuspendAfterSeconds decodes as float64 because a JSON Schema integer may be written 60.0; the schema bounds
	// it.
	IdleSuspendAfterSeconds *float64 `json:"idle_suspend_after_seconds,omitempty"`
	LauncherGrants          []string `json:"launcher_grants,omitempty"`
}

// SandboxRef is one incarnation of a Sandbox.
type SandboxRef struct {
	Name string    `json:"name"`
	UID  types.UID `json:"uid"`
}

// Launched is the result of launch_sandbox.
type Launched struct {
	Sandbox SandboxRef `json:"sandbox"`
	// LauncherGrants are what its launcher may do to it without asking again.
	LauncherGrants []string `json:"launcher_grants"`
}

// launcherName names the PolicySet and PolicyBinding of a Sandbox's launcher grants. A Sandbox's name has no dot, so it
// never names the PolicyBinding the Sandbox Controller writes for a Sandbox's own account, which takes the Sandbox's
// name.
func launcherName(sandbox string) string { return sandbox + ".launcher" }

// launch creates the Sandbox args describe and writes its launcher grants for caller. A launch the same caller made
// with the same arguments, whatever became of its attempt, is adopted and its grants completed.
func (e *Executor) launch(ctx context.Context, caller action.ServiceAccount, args launchArgs) (Launched, error) {
	for _, list := range [][]string{args.PolicySets, args.InboxTags, args.LauncherGrants} {
		slices.Sort(list)
	}
	intent, err := json.Marshal(struct {
		Caller action.ServiceAccount `json:"caller"`
		Launch launchArgs            `json:"launch"`
	}{caller, args})
	if err != nil {
		return Launched{}, err
	}
	sandbox, err := e.launched(ctx, args.Name, string(intent))
	if err == nil && sandbox == nil {
		sandbox, err = e.create(ctx, args, string(intent))
	}
	if err != nil {
		return Launched{}, err
	}
	if err := e.grant(ctx, caller, sandbox, args.LauncherGrants); err != nil {
		var refused *refusal
		if errors.As(err, &refused) {
			refused.message = fmt.Sprintf("Sandbox %q was launched, but its launcher grants were not written: %s",
				sandbox.Name, refused.message)
		}
		return Launched{}, err
	}
	return Launched{
		Sandbox: SandboxRef{Name: sandbox.Name, UID: sandbox.UID}, LauncherGrants: append([]string{}, args.LauncherGrants...),
	}, nil
}

// launched is the Sandbox of the name that a launch with this intent made; nil when there is none. Another one of the
// name is refused.
func (e *Executor) launched(ctx context.Context, name, intent string) (*sandboxapi.Sandbox, error) {
	var sandbox sandboxapi.Sandbox
	err := e.kube.Get(ctx, client.ObjectKey{Namespace: e.binding.Namespace, Name: name}, &sandbox)
	switch {
	case apierrors.IsNotFound(err):
		return nil, nil
	case err != nil:
		return nil, kubeError("reading the Sandbox", err)
	case sandbox.DeletionTimestamp != nil || sandbox.Annotations[sandboxapi.CreateIntentAnnotation] != intent:
		return nil, refuse("sandbox_exists", "a Sandbox named %q exists that this caller did not launch with these arguments", name)
	}
	return &sandbox, nil
}

func (e *Executor) create(ctx context.Context, args launchArgs, intent string) (*sandboxapi.Sandbox, error) {
	policySets := slices.Compact(slices.Sorted(slices.Values(slices.Concat(e.binding.DefaultPolicySets, args.PolicySets))))
	if err := e.requirePolicySets(ctx, policySets); err != nil {
		return nil, err
	}
	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{
			Name: args.Name, Namespace: e.binding.Namespace,
			Annotations: map[string]string{sandboxapi.CreateIntentAnnotation: intent},
		},
		Spec: sandboxapi.SandboxSpec{
			Template: args.Template, Running: true, WakeOnSend: args.WakeOnSend, PolicySets: policySets,
			InboxTags: args.InboxTags, Bootstrap: args.Bootstrap,
		},
	}
	if seconds := args.IdleSuspendAfterSeconds; seconds != nil {
		sandbox.Spec.IdleSuspendAfter = &metav1.Duration{Duration: time.Duration(*seconds) * time.Second}
	}
	err := e.kube.Create(ctx, sandbox)
	if apierrors.IsAlreadyExists(err) {
		// Created since launched read it: by an attempt at this launch, or by another.
		existing, err := e.launched(ctx, args.Name, intent)
		if err == nil && existing == nil {
			err = refuse("sandbox_exists", "a Sandbox named %q was created and deleted while this launch ran", args.Name)
		}
		return existing, err
	}
	if err != nil {
		return nil, kubeError("creating the Sandbox", err)
	}
	return sandbox, nil
}

// requirePolicySets refuses a PolicySet that does not exist, and one that is a launcher grant: bound to the new
// Sandbox's account, it would give that account what another Sandbox's launcher was granted.
func (e *Executor) requirePolicySets(ctx context.Context, names []string) error {
	var missing, granted []string
	for _, name := range names {
		var set policyapi.PolicySet
		err := e.kube.Get(ctx, client.ObjectKey{Namespace: e.binding.Namespace, Name: name}, &set)
		switch {
		case apierrors.IsNotFound(err):
			missing = append(missing, name)
		case err != nil:
			return kubeError("reading PolicySets", err)
		case slices.ContainsFunc(set.OwnerReferences, isSandbox):
			granted = append(granted, name)
		}
	}
	if len(missing) > 0 {
		return refuse("unknown_policy_set", "no PolicySet exists named %q", missing)
	}
	if len(granted) > 0 {
		return refuse("launcher_policy_set", "PolicySets %q are other Sandboxes' launcher grants", granted)
	}
	return nil
}

func isSandbox(owner metav1.OwnerReference) bool {
	return owner.APIVersion == sandboxapi.GroupVersion.String() && owner.Kind == sandboxapi.SandboxKind
}

// grant writes the launcher grants on sandbox for caller, when there are any: a PolicySet of their rules and a
// PolicyBinding of it to caller. Both are owned by the Sandbox, so deleting it revokes them.
func (e *Executor) grant(ctx context.Context, caller action.ServiceAccount, sandbox *sandboxapi.Sandbox, grants []string) error {
	if len(grants) == 0 {
		return nil
	}
	rules, err := e.launcherRules(SandboxRef{Name: sandbox.Name, UID: sandbox.UID}, grants)
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
		Subject:    policyapi.ServiceAccountRef{Namespace: caller.Namespace, Name: caller.Name},
		PolicySets: []string{meta.Name},
	}}, sandbox.UID)
}

// launcherRules are the rules granting what grants names on sandbox: send_message as the Notification Service's inbox
// rule, and this group's Actions as an auto-approval of the requests whose `sandbox` argument is that incarnation.
func (e *Executor) launcherRules(sandbox SandboxRef, grants []string) ([]policyapi.Rule, error) {
	var rules []policyapi.Rule
	actions := slices.DeleteFunc(slices.Clone(grants), func(grant string) bool { return grant == grantSendMessage })
	if len(actions) < len(grants) {
		rules = append(rules, policyapi.Rule{
			Type: inboxAccessRule, Operations: []string{grantSendMessage},
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
