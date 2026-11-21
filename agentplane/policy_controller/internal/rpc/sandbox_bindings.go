package rpc

import (
	"context"
	"log/slog"
	"slices"
	"strings"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func (s *Service) GrantSandboxPolicyBinding(ctx context.Context, request *policypb.GrantSandboxPolicyBindingRequest) (*policypb.GrantSandboxPolicyBindingResponse, error) {
	target, err := s.requestTarget(request.GetSandboxNamespace(), request.GetSandboxName(), request.GetSandboxUid())
	if err != nil {
		return nil, err
	}
	if len(request.GetPolicySets()) == 0 {
		return nil, status.Error(codes.InvalidArgument, "at least one PolicySet is required")
	}
	if err := validSandboxName(target.Name); err != nil {
		return nil, err
	}
	identity, err := s.sandboxIdentity(ctx, target)
	if err != nil {
		return nil, err
	}
	if err := s.requireKnownPolicySets(ctx, request.GetPolicySets()); err != nil {
		return nil, err
	}
	controller, blockOwnerDeletion := false, false
	binding := &policyapi.PolicyBinding{
		TypeMeta: metav1.TypeMeta{APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "PolicyBinding"},
		ObjectMeta: metav1.ObjectMeta{
			Namespace: s.namespace, GenerateName: target.Name + "-",
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
				Name: target.Name, UID: types.UID(target.Uid),
				Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
			}},
		},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.Subject{Identity: &policyapi.IdentityRef{Name: identity.Name}},
			PolicySets: slices.Clone(request.GetPolicySets()),
		},
	}
	if err := s.createPolicyBinding(ctx, binding, request.GetExpiresAt()); err != nil {
		return nil, err
	}
	// The owner reference carries the Sandbox UID, but the subject names the Identity by name only.
	// Recheck after create to catch a Sandbox replacement, a rebind or an Identity replacement
	// between the first read and the write. This is detection and cleanup, not an atomic fence.
	current, err := s.sandboxIdentity(ctx, target)
	if err != nil || current.UID != identity.UID {
		s.cleanupCreatedBinding(ctx, binding)
		if err != nil && status.Code(err) == codes.Unavailable {
			return nil, err
		}
		return nil, status.Error(codes.NotFound, "Sandbox not found")
	}
	message, err := bindingMessage(binding)
	if err != nil {
		slog.ErrorContext(ctx, "PolicyService created an invalid PolicyBinding", "error", err)
		return nil, status.Error(codes.Internal, "PolicyBinding response is invalid")
	}
	return &policypb.GrantSandboxPolicyBindingResponse{Binding: message}, nil
}

func (s *Service) createPolicyBinding(ctx context.Context, binding *policyapi.PolicyBinding, expiresAt *timestamppb.Timestamp) error {
	if expiresAt != nil {
		if err := expiresAt.CheckValid(); err != nil {
			return status.Error(codes.InvalidArgument, "expires_at must be a valid timestamp")
		}
		converted := metav1.NewTime(expiresAt.AsTime().UTC())
		binding.Spec.ExpiresAt = &converted
	}
	if err := s.objects.Create(ctx, binding); err != nil {
		slog.ErrorContext(ctx, "PolicyService PolicyBinding creation failed", "error", err)
		if apierrors.IsAlreadyExists(err) {
			return status.Error(codes.Aborted, "PolicyBinding creation conflicted; try again")
		}
		return status.Error(codes.Unavailable, "PolicyBinding could not be created")
	}
	return nil
}

func (s *Service) cleanupCreatedBinding(ctx context.Context, binding *policyapi.PolicyBinding) {
	if binding.GetName() == "" || binding.GetUID() == "" || binding.GetResourceVersion() == "" {
		slog.ErrorContext(ctx, "PolicyService cannot safely clean up a grant after Sandbox identity changed", "binding", binding.GetName())
		return
	}
	uid, resourceVersion := binding.UID, binding.ResourceVersion
	cleanupContext, cancel := context.WithTimeout(context.WithoutCancel(ctx), writeTimeout)
	defer cancel()
	if err := s.objects.Delete(cleanupContext, binding, client.Preconditions{UID: &uid, ResourceVersion: &resourceVersion}); err != nil && !apierrors.IsNotFound(err) {
		slog.ErrorContext(ctx, "PolicyService stale PolicyBinding cleanup failed", "binding", binding.Name, "error", err)
	}
}

func (s *Service) RevokeSandboxPolicyBinding(ctx context.Context, request *policypb.RevokeSandboxPolicyBindingRequest) (*policypb.RevokeSandboxPolicyBindingResponse, error) {
	target, err := s.requestTarget(request.GetSandboxNamespace(), request.GetSandboxName(), request.GetSandboxUid())
	if err != nil {
		return nil, err
	}
	if request.GetBindingName() == "" || request.GetBindingUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "PolicyBinding name and UID are required")
	}
	if err := validSandboxName(target.Name); err != nil {
		return nil, err
	}
	identity, err := s.sandboxIdentity(ctx, target)
	if err != nil {
		return nil, err
	}
	binding := &policyapi.PolicyBinding{}
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: request.GetBindingName()}, binding); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.NotFound, "PolicyBinding not found")
		}
		slog.ErrorContext(ctx, "PolicyService PolicyBinding read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "PolicyBinding is unavailable")
	}
	if string(binding.UID) != request.GetBindingUid() {
		return nil, status.Error(codes.Aborted, "PolicyBinding changed; refresh the Sandbox policy view")
	}
	if _, fromFlux := binding.Labels[policyapi.FluxKustomizationNameLabel]; fromFlux {
		return nil, status.Error(codes.Aborted, "PolicyBinding is managed by Flux")
	}
	for _, owner := range binding.OwnerReferences {
		if owner.Kind != sandboxapi.SandboxKind {
			continue
		}
		if owner.Controller != nil && *owner.Controller {
			return nil, status.Error(codes.Aborted, "PolicyBinding is owned by the Sandbox controller")
		}
		if owner.APIVersion != sandboxapi.GroupVersion.String() || owner.Name != target.Name || string(owner.UID) != target.Uid {
			return nil, status.Error(codes.Aborted, "PolicyBinding belongs to another Sandbox incarnation")
		}
	}
	if binding.Spec.Subject.Identity == nil || binding.Spec.Subject.Identity.Name != identity.Name {
		return nil, status.Error(codes.Aborted, "PolicyBinding belongs to another Identity")
	}
	if binding.UID == "" || binding.ResourceVersion == "" {
		return nil, status.Error(codes.Aborted, "PolicyBinding identity is unavailable; refresh the Sandbox policy view")
	}
	uid, resourceVersion := binding.UID, binding.ResourceVersion
	if err := s.objects.Delete(ctx, binding, client.Preconditions{UID: &uid, ResourceVersion: &resourceVersion}); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.NotFound, "PolicyBinding not found")
		}
		if apierrors.IsConflict(err) {
			return nil, status.Error(codes.Aborted, "PolicyBinding changed; refresh the Sandbox policy view")
		}
		slog.ErrorContext(ctx, "PolicyService PolicyBinding deletion failed", "error", err)
		return nil, status.Error(codes.Unavailable, "PolicyBinding could not be deleted")
	}
	return &policypb.RevokeSandboxPolicyBindingResponse{}, nil
}

func (s *Service) requestTarget(namespace, name, uid string) (*identitypb.EnvironmentTarget, error) {
	if namespace == "" || name == "" || uid == "" {
		return nil, status.Error(codes.InvalidArgument, "Sandbox namespace, name, and UID are required")
	}
	if namespace != s.namespace {
		return nil, status.Error(codes.NotFound, "Sandbox not found")
	}
	return &identitypb.EnvironmentTarget{Namespace: namespace, Name: name, Uid: uid}, nil
}

func validSandboxName(name string) error {
	if !sandboxapi.IsValidSandboxName(name) {
		return status.Error(codes.InvalidArgument, "invalid Sandbox name")
	}
	return nil
}

// sandboxIdentity is the live Identity the exact Sandbox incarnation acts as: the one its
// spec.identity names. An operator grant on a Sandbox binds that Identity.
func (s *Service) sandboxIdentity(ctx context.Context, target *identitypb.EnvironmentTarget) (*policyapi.Identity, error) {
	view, err := s.sandboxes.Get(ctx, target.GetName())
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, status.Error(codes.NotFound, "Sandbox not found")
		}
		slog.ErrorContext(ctx, "PolicyService Sandbox read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox is unavailable")
	}
	if view.Identity.GetUid() != target.GetUid() || view.Resource.DeletionTimestamp != nil {
		return nil, status.Error(codes.NotFound, "Sandbox not found")
	}
	if view.Resource.Spec.Identity == nil {
		return nil, status.Error(codes.FailedPrecondition, "Sandbox acts as no Identity")
	}
	identity := &policyapi.Identity{}
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: view.Resource.Spec.Identity.Name}, identity); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.FailedPrecondition, "the Sandbox's Identity does not exist")
		}
		slog.ErrorContext(ctx, "PolicyService Identity read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Identity is unavailable")
	}
	if !identity.DeletionTimestamp.IsZero() {
		return nil, status.Error(codes.FailedPrecondition, "the Sandbox's Identity is being deleted")
	}
	return identity, nil
}

func (s *Service) requireKnownPolicySets(ctx context.Context, names []string) error {
	var list policyapi.PolicySetList
	if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
		slog.ErrorContext(ctx, "PolicyService PolicySet read failed", "error", err)
		return status.Error(codes.Unavailable, "PolicySet inventory is unavailable")
	}
	known := sets.New[string]()
	for _, set := range list.Items {
		known.Insert(set.Name)
	}
	missing, seen := sets.New[string](), sets.New[string]()
	for _, name := range names {
		if name == "" {
			return status.Error(codes.InvalidArgument, "PolicySet names must not be empty")
		}
		if seen.Has(name) {
			return status.Error(codes.InvalidArgument, "PolicySet names must be unique")
		}
		seen.Insert(name)
		if !known.Has(name) {
			missing.Insert(name)
		}
	}
	if len(missing) == 0 {
		return nil
	}
	return status.Error(codes.InvalidArgument, "unknown PolicySets: "+strings.Join(sets.List(missing), ", "))
}
