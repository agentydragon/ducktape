package rpc

import (
	"context"
	"log/slog"
	"slices"
	"strings"

	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func (s *Service) DeleteIdentity(ctx context.Context, request *policypb.DeleteIdentityRequest) (*policypb.DeleteIdentityResponse, error) {
	if request.GetNamespace() == "" || request.GetName() == "" || request.GetUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "Identity namespace, name, and UID are required")
	}
	if request.GetNamespace() != s.namespace {
		return nil, status.Error(codes.NotFound, "Identity not found")
	}
	identity := &policyapi.Identity{}
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: request.GetName()}, identity); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.NotFound, "Identity not found")
		}
		slog.ErrorContext(ctx, "PolicyService Identity read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Identity is unavailable")
	}
	if string(identity.UID) != request.GetUid() || identity.ResourceVersion == "" {
		return nil, status.Error(codes.Aborted, "Identity changed; refresh the identities view")
	}
	if _, fromFlux := identity.Labels[policyapi.FluxKustomizationNameLabel]; fromFlux {
		return nil, status.Error(codes.FailedPrecondition, "Identity is managed by Flux")
	}
	var bindings policyapi.PolicyBindingList
	if err := s.objects.List(ctx, &bindings, client.InNamespace(s.namespace)); err != nil {
		slog.ErrorContext(ctx, "PolicyService PolicyBinding read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "PolicyBinding inventory is unavailable")
	}
	var referencing []string
	for _, binding := range bindings.Items {
		bound := binding.Spec.Subject.Identity
		if bound == nil || bound.Name != identity.Name || ownedBy(binding.OwnerReferences, identity.UID) {
			continue
		}
		referencing = append(referencing, binding.Name)
	}
	slices.Sort(referencing)
	// Match the resolver: name-only subjects follow the live Identity.
	var credentials []string
	for _, namespace := range s.workloadNamespaces {
		var list workloadapi.WorkloadCredentialList
		if err := s.objects.List(ctx, &list, client.InNamespace(namespace)); err != nil {
			slog.ErrorContext(ctx, "PolicyService WorkloadCredential read failed", "namespace", namespace, "error", err)
			return nil, status.Error(codes.Unavailable, "WorkloadCredential inventory is unavailable")
		}
		for _, credential := range list.Items {
			if subject := credential.Spec.Subject.Identity; subject != nil && subject.Name == identity.Name &&
				(subject.UID == "" || subject.UID == identity.UID) {
				credentials = append(credentials, credential.Namespace+"/"+credential.Name)
			}
		}
	}
	slices.Sort(credentials)
	if len(referencing) > 0 || len(credentials) > 0 {
		failure := &errdetails.PreconditionFailure{}
		var named []string
		if len(referencing) > 0 {
			named = append(named, "PolicyBindings it does not own: "+strings.Join(referencing, ", "))
		}
		for _, name := range referencing {
			failure.Violations = append(failure.Violations, &errdetails.PreconditionFailure_Violation{
				Type: "PolicyBinding", Subject: name, Description: "names this Identity and is not owned by it",
			})
		}
		if len(credentials) > 0 {
			named = append(named, "WorkloadCredentials: "+strings.Join(credentials, ", "))
		}
		for _, name := range credentials {
			failure.Violations = append(failure.Violations, &errdetails.PreconditionFailure_Violation{
				Type: workloadapi.Kind, Subject: name, Description: "acts as this Identity",
			})
		}
		refusal, err := status.New(codes.FailedPrecondition, "Identity is named by "+strings.Join(named, "; and by ")).WithDetails(failure)
		if err != nil {
			return nil, status.Error(codes.Internal, "Identity refusal could not be encoded")
		}
		return nil, refusal.Err()
	}
	uid, resourceVersion := identity.UID, identity.ResourceVersion
	if err := s.objects.Delete(ctx, identity, client.Preconditions{UID: &uid, ResourceVersion: &resourceVersion}); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.NotFound, "Identity not found")
		}
		if apierrors.IsConflict(err) {
			return nil, status.Error(codes.Aborted, "Identity changed; refresh the identities view")
		}
		slog.ErrorContext(ctx, "PolicyService Identity deletion failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Identity could not be deleted")
	}
	return &policypb.DeleteIdentityResponse{}, nil
}

func (s *Service) CreateIdentity(ctx context.Context, request *policypb.CreateIdentityRequest) (*policypb.CreateIdentityResponse, error) {
	if request.GetName() == "" || request.GetDescription() == "" {
		return nil, status.Error(codes.InvalidArgument, "Identity name and description are required")
	}
	if errs := validation.IsDNS1123Subdomain(request.GetName()); len(errs) > 0 {
		return nil, status.Error(codes.InvalidArgument, "Identity name must be a DNS subdomain: "+strings.Join(errs, "; "))
	}
	identity := &policyapi.Identity{
		TypeMeta:   metav1.TypeMeta{APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "Identity"},
		ObjectMeta: metav1.ObjectMeta{Namespace: s.namespace, Name: request.GetName()},
		Spec:       policyapi.IdentitySpec{Description: request.GetDescription()},
	}
	if err := s.objects.Create(ctx, identity); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return nil, status.Error(codes.AlreadyExists, "an Identity of that name exists")
		}
		if apierrors.IsInvalid(err) {
			return nil, status.Error(codes.InvalidArgument, "Identity is invalid")
		}
		slog.ErrorContext(ctx, "PolicyService Identity creation failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Identity could not be created")
	}
	message, err := identityMessage(identity)
	if err != nil {
		slog.ErrorContext(ctx, "PolicyService created an invalid Identity", "error", err)
		return nil, status.Error(codes.Internal, "Identity response is invalid")
	}
	return &policypb.CreateIdentityResponse{Identity: message}, nil
}

func (s *Service) GrantIdentityPolicyBinding(ctx context.Context, request *policypb.GrantIdentityPolicyBindingRequest) (*policypb.GrantIdentityPolicyBindingResponse, error) {
	if request.GetIdentityName() == "" || request.GetIdentityUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "Identity name and UID are required")
	}
	if len(request.GetPolicySets()) == 0 {
		return nil, status.Error(codes.InvalidArgument, "at least one PolicySet is required")
	}
	identity := &policyapi.Identity{}
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: request.GetIdentityName()}, identity); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.NotFound, "Identity not found")
		}
		slog.ErrorContext(ctx, "PolicyService Identity read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Identity is unavailable")
	}
	if string(identity.UID) != request.GetIdentityUid() || !identity.DeletionTimestamp.IsZero() {
		return nil, status.Error(codes.NotFound, "Identity not found")
	}
	if err := s.requireKnownPolicySets(ctx, request.GetPolicySets()); err != nil {
		return nil, err
	}
	var expiresAt *metav1.Time
	if value := request.GetExpiresAt(); value != nil {
		if err := value.CheckValid(); err != nil {
			return nil, status.Error(codes.InvalidArgument, "expires_at must be a valid timestamp")
		}
		converted := metav1.NewTime(value.AsTime().UTC())
		expiresAt = &converted
	}
	controller, blockOwnerDeletion := false, false
	binding := &policyapi.PolicyBinding{
		TypeMeta: metav1.TypeMeta{APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "PolicyBinding"},
		ObjectMeta: metav1.ObjectMeta{
			Namespace: s.namespace, GenerateName: identity.Name + "-",
			// Owned by the Identity incarnation: deleting it deletes this grant (DeleteIdentity does
			// not count owned bindings), and a same-named successor inherits nothing.
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "Identity",
				Name: identity.Name, UID: identity.UID,
				Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
			}},
		},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.Subject{Identity: &policyapi.IdentityRef{Name: identity.Name}},
			PolicySets: slices.Clone(request.GetPolicySets()), ExpiresAt: expiresAt,
		},
	}
	if err := s.objects.Create(ctx, binding); err != nil {
		slog.ErrorContext(ctx, "PolicyService PolicyBinding creation failed", "error", err)
		if apierrors.IsAlreadyExists(err) {
			return nil, status.Error(codes.Aborted, "PolicyBinding creation conflicted; try again")
		}
		return nil, status.Error(codes.Unavailable, "PolicyBinding could not be created")
	}
	message, err := bindingMessage(binding)
	if err != nil {
		slog.ErrorContext(ctx, "PolicyService created an invalid PolicyBinding", "error", err)
		return nil, status.Error(codes.Internal, "PolicyBinding response is invalid")
	}
	return &policypb.GrantIdentityPolicyBindingResponse{Binding: message}, nil
}

func ownedBy(owners []metav1.OwnerReference, uid types.UID) bool {
	return slices.ContainsFunc(owners, func(owner metav1.OwnerReference) bool { return owner.UID == uid })
}
