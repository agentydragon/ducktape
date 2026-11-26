package rpc

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log/slog"
	"slices"
	"strings"
	"unicode/utf8"

	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// GetIdentity reads through the uncached client, so it sees an Identity as soon as CreateIdentity returns.
func (s *Service) GetIdentity(ctx context.Context, request *policypb.GetIdentityRequest) (*policypb.GetIdentityResponse, error) {
	if err := s.requireReader(ctx); err != nil {
		return nil, err
	}
	if request.GetName() == "" {
		return nil, status.Error(codes.InvalidArgument, "Identity name is required")
	}
	identity := &policyapi.Identity{}
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: request.GetName()}, identity); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.NotFound, "Identity not found")
		}
		slog.ErrorContext(ctx, "PolicyService Identity read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Identity is unavailable")
	}
	message, err := identityMessage(identity)
	if err != nil {
		slog.ErrorContext(ctx, "PolicyService read an invalid Identity", "error", err)
		return nil, status.Error(codes.Internal, "Identity response is invalid")
	}
	return &policypb.GetIdentityResponse{Identity: message}, nil
}

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
	if request.GetValidateOnly() {
		return &policypb.DeleteIdentityResponse{}, nil
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
	unkeyed := proto.CloneOf(request)
	unkeyed.IdempotencyKey = ""
	record, err := idempotencyRecord(request.GetIdempotencyKey(), unkeyed)
	if err != nil {
		return nil, err
	}
	identity := &policyapi.Identity{
		TypeMeta:   metav1.TypeMeta{APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "Identity"},
		ObjectMeta: metav1.ObjectMeta{Namespace: s.namespace, Name: request.GetName(), Annotations: record},
		Spec:       policyapi.IdentitySpec{Description: request.GetDescription()},
	}
	if err := s.objects.Create(ctx, identity); err != nil {
		switch {
		case apierrors.IsAlreadyExists(err) && record != nil:
			if identity, err = s.retriedIdentity(ctx, request.GetName(), record); err != nil {
				return nil, err
			}
		case apierrors.IsAlreadyExists(err):
			return nil, status.Error(codes.AlreadyExists, "an Identity of that name exists")
		case apierrors.IsInvalid(err):
			return nil, status.Error(codes.InvalidArgument, "Identity is invalid")
		default:
			slog.ErrorContext(ctx, "PolicyService Identity creation failed", "error", err)
			return nil, status.Error(codes.Unavailable, "Identity could not be created")
		}
	}
	message, err := identityMessage(identity)
	if err != nil {
		slog.ErrorContext(ctx, "PolicyService created an invalid Identity", "error", err)
		return nil, status.Error(codes.Internal, "Identity response is invalid")
	}
	return &policypb.CreateIdentityResponse{Identity: message}, nil
}

// retriedIdentity is the Identity of name that an earlier CreateIdentity carrying record created, while it is not
// being deleted.
func (s *Service) retriedIdentity(ctx context.Context, name string, record map[string]string) (*policyapi.Identity, error) {
	identity := &policyapi.Identity{}
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: name}, identity); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.Aborted, "Identity changed; try again")
		}
		slog.ErrorContext(ctx, "PolicyService Identity read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Identity is unavailable")
	}
	if !recorded(identity.Annotations, record) {
		return nil, status.Error(codes.AlreadyExists, "an Identity of that name exists and is not this idempotency_key's")
	}
	if !identity.DeletionTimestamp.IsZero() {
		return nil, status.Error(codes.AlreadyExists, "this idempotency_key's Identity is being deleted")
	}
	return identity, nil
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
	unkeyed := proto.CloneOf(request)
	unkeyed.IdempotencyKey = ""
	record, err := idempotencyRecord(request.GetIdempotencyKey(), unkeyed)
	if err != nil {
		return nil, err
	}
	controller, blockOwnerDeletion := false, false
	binding := &policyapi.PolicyBinding{
		TypeMeta: metav1.TypeMeta{APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "PolicyBinding"},
		ObjectMeta: metav1.ObjectMeta{
			Namespace: s.namespace, GenerateName: identity.Name + "-", Annotations: record,
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
			PolicySets: slices.Clone(request.GetPolicySets()),
		},
	}
	if record != nil {
		binding.GenerateName, binding.Name = "", keyedBindingName(identity, request.GetIdempotencyKey())
	}
	if err := s.createPolicyBinding(ctx, binding, request.GetExpiresAt()); err != nil {
		if record == nil || !errors.Is(err, errBindingNameTaken) {
			return nil, err
		}
		if binding, err = s.retriedIdentityBinding(ctx, binding.Name, identity.UID, record); err != nil {
			return nil, err
		}
	}
	message, err := bindingMessage(binding)
	if err != nil {
		slog.ErrorContext(ctx, "PolicyService created an invalid PolicyBinding", "error", err)
		return nil, status.Error(codes.Internal, "PolicyBinding response is invalid")
	}
	return &policypb.GrantIdentityPolicyBindingResponse{Binding: message}, nil
}

// keyedBindingName is the one name a grant keyed by key on identity takes, so that a retry meets the binding the
// first call created. It derives from the Identity's UID as well, so a same-named successor's grants never collide
// with a predecessor's that garbage collection has yet to remove.
func keyedBindingName(identity *policyapi.Identity, key string) string {
	sum := sha256.Sum256([]byte(string(identity.UID) + "\x00" + key))
	suffix := "-" + hex.EncodeToString(sum[:8])
	prefix := identity.Name
	if limit := validation.DNS1123SubdomainMaxLength - len(suffix); len(prefix) > limit {
		prefix = strings.TrimRight(prefix[:limit], ".-")
	}
	return prefix + suffix
}

// retriedIdentityBinding is the binding of name that an earlier grant carrying record created on the Identity
// incarnation owner, while it is not being deleted.
func (s *Service) retriedIdentityBinding(ctx context.Context, name string, owner types.UID, record map[string]string) (*policyapi.PolicyBinding, error) {
	binding := &policyapi.PolicyBinding{}
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: name}, binding); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.Aborted, "PolicyBinding changed; try again")
		}
		slog.ErrorContext(ctx, "PolicyService PolicyBinding read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "PolicyBinding is unavailable")
	}
	if !ownedBy(binding.OwnerReferences, owner) || !recorded(binding.Annotations, record) {
		return nil, status.Error(codes.AlreadyExists, "this idempotency_key granted the Identity another request")
	}
	if !binding.DeletionTimestamp.IsZero() {
		return nil, status.Error(codes.AlreadyExists, "this idempotency_key's PolicyBinding is being deleted")
	}
	return binding, nil
}

// idempotencyKeyAnnotation and requestDigestAnnotation are PolicyService's record, on an object a keyed request
// created, of its idempotency_key and a digest of the rest of the request. They stand in for a uniqueness constraint
// while Identities and PolicyBindings are custom resources; should the record move into a database, the key becomes
// a unique key there and the digest a column beside it.
const (
	idempotencyKeyAnnotation = "agentplane.allegedly.works/idempotency-key"
	requestDigestAnnotation  = "agentplane.allegedly.works/request-digest"
	maxIdempotencyKeyLength  = 200
)

// idempotencyRecord is the annotations recording a request keyed by key whose other fields are unkeyed, or nil
// without a key.
func idempotencyRecord(key string, unkeyed proto.Message) (map[string]string, error) {
	if key == "" {
		return nil, nil
	}
	if utf8.RuneCountInString(key) > maxIdempotencyKeyLength {
		return nil, status.Errorf(codes.InvalidArgument, "idempotency_key must be 1 to %d characters", maxIdempotencyKeyLength)
	}
	// Deterministic marshalling is stable for one schema. A field added later is absent until set, so it leaves the
	// digest of a request that does not set it unchanged.
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(unkeyed)
	if err != nil {
		return nil, status.Error(codes.Internal, "request could not be digested")
	}
	digest := sha256.Sum256(encoded)
	return map[string]string{idempotencyKeyAnnotation: key, requestDigestAnnotation: hex.EncodeToString(digest[:])}, nil
}

func recorded(annotations, record map[string]string) bool {
	return annotations[idempotencyKeyAnnotation] == record[idempotencyKeyAnnotation] &&
		annotations[requestDigestAnnotation] == record[requestDigestAnnotation]
}

func ownedBy(owners []metav1.OwnerReference, uid types.UID) bool {
	return slices.ContainsFunc(owners, func(owner metav1.OwnerReference) bool { return owner.UID == uid })
}
