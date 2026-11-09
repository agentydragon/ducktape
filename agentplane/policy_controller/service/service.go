// Package service serves Policy resource watches from the Kubernetes API.
package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	resourcepb "github.com/agentydragon/ducktape/agentplane/policy/api/resourcev1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	kubernetespb "github.com/agentydragon/ducktape/agentplane/proto/kubernetes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/apimachinery/pkg/watch"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	recheckInterval = 5 * time.Second
	writeTimeout    = 5 * time.Second
	snapshotChunk   = 64
	// fluxNameLabel marks an object Flux applies from git, which Flux would recreate if deleted.
	fluxNameLabel = "kustomize.toolkit.fluxcd.io/name"
)

type Service struct {
	policypb.UnimplementedPolicyServiceServer
	namespace string
	objects   client.WithWatch
	sandboxes *sandboxreader.Reader
	readers   map[auth.Account]struct{}
	writers   []auth.Account
}

func New(namespace string, objects client.WithWatch, readers, writers []auth.Account) (*Service, error) {
	if namespace == "" || objects == nil {
		return nil, errors.New("policy namespace and Kubernetes watch client are required")
	}
	sandboxes, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	allowed := make(map[auth.Account]struct{}, len(readers))
	for _, account := range readers {
		allowed[account] = struct{}{}
	}
	return &Service{namespace: namespace, objects: objects, sandboxes: sandboxes, readers: allowed, writers: slices.Clone(writers)}, nil
}

func (s *Service) Methods(readResolver, writeResolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccount(readResolver)
	return auth.Methods{
		policypb.PolicyService_GrantSandboxPolicyBinding_FullMethodName:  auth.Accounts(writeResolver, s.writers),
		policypb.PolicyService_RevokeSandboxPolicyBinding_FullMethodName: auth.Accounts(writeResolver, s.writers),
		policypb.PolicyService_WatchPolicyBindings_FullMethodName:        rule,
		policypb.PolicyService_WatchPolicySets_FullMethodName:            rule,
		policypb.PolicyService_ListPolicySetNames_FullMethodName:         rule,
		policypb.PolicyService_WatchIdentities_FullMethodName:            rule,
		policypb.PolicyService_DeleteIdentity_FullMethodName:             auth.Accounts(writeResolver, s.writers),
	}
}

func (s *Service) caller(ctx context.Context) (auth.Account, bool, error) {
	identity, err := auth.EffectiveCaller(ctx)
	if err != nil {
		return auth.Account{}, false, err
	}
	if identity == nil || identity.GetAccount().GetNamespace() == "" || identity.GetAccount().GetName() == "" || identity.GetUid() == "" {
		return auth.Account{}, false, status.Error(codes.Unauthenticated, "incomplete caller")
	}
	caller := auth.Account{Namespace: identity.GetAccount().GetNamespace(), Name: identity.GetAccount().GetName()}
	_, broad := s.readers[caller]
	return caller, broad, nil
}

func (s *Service) GrantSandboxPolicyBinding(ctx context.Context, request *policypb.GrantSandboxPolicyBindingRequest) (*policypb.GrantSandboxPolicyBindingResponse, error) {
	if request == nil {
		return nil, status.Error(codes.InvalidArgument, "Sandbox namespace, name, and UID are required")
	}
	target, err := s.requestTarget(request.GetSandboxNamespace(), request.GetSandboxName(), request.GetSandboxUid())
	if err != nil {
		return nil, err
	}
	if request == nil || len(request.GetPolicySets()) == 0 {
		return nil, status.Error(codes.InvalidArgument, "at least one PolicySet is required")
	}
	if err := validSandboxName(target.Name); err != nil {
		return nil, err
	}
	account, err := s.executionAccount(ctx, target)
	if err != nil {
		return nil, err
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
			Namespace: s.namespace, GenerateName: target.Name + "-",
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
				Name: target.Name, UID: types.UID(target.Uid),
				Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
			}},
		},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.Subject{Namespace: account.GetAccount().GetNamespace(), Name: account.GetAccount().GetName()},
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
	// The owner reference carries the Sandbox UID, but PolicyBinding subjects name only a
	// ServiceAccount. Recheck after create to catch a Sandbox or account replacement between the
	// first read and the write. This is detection and cleanup, not an atomic cross-object fence.
	currentAccount, err := s.executionAccount(ctx, target)
	if err != nil || currentAccount.GetUid() != account.GetUid() {
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
	if request == nil {
		return nil, status.Error(codes.InvalidArgument, "Sandbox namespace, name, and UID are required")
	}
	target, err := s.requestTarget(request.GetSandboxNamespace(), request.GetSandboxName(), request.GetSandboxUid())
	if err != nil {
		return nil, err
	}
	if request == nil || request.GetBindingName() == "" || request.GetBindingUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "PolicyBinding name and UID are required")
	}
	if err := validSandboxName(target.Name); err != nil {
		return nil, err
	}
	account, err := s.executionAccount(ctx, target)
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
	if _, fromFlux := binding.Labels[fluxNameLabel]; fromFlux {
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
	if binding.Spec.Subject.Namespace != account.GetAccount().GetNamespace() || binding.Spec.Subject.Name != account.GetAccount().GetName() {
		return nil, status.Error(codes.Aborted, "PolicyBinding belongs to another ServiceAccount")
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
	if len(name) > 57 || len(validation.IsDNS1123Label(name)) > 0 {
		return status.Error(codes.InvalidArgument, "invalid Sandbox name")
	}
	return nil
}

func (s *Service) executionAccount(ctx context.Context, target *identitypb.EnvironmentTarget) (*identitypb.ServiceAccountIdentity, error) {
	view, err := s.sandboxes.Get(ctx, target.GetName())
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, status.Error(codes.NotFound, "Sandbox not found")
		}
		slog.ErrorContext(ctx, "PolicyService Sandbox identity read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox identity is unavailable")
	}
	if view.Identity.GetUid() != target.GetUid() || view.Resource.DeletionTimestamp != nil {
		return nil, status.Error(codes.NotFound, "Sandbox not found")
	}
	account, found, err := s.sandboxes.ExecutionAccount(ctx, target)
	if err != nil {
		slog.ErrorContext(ctx, "PolicyService Sandbox ServiceAccount read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox ServiceAccount is unavailable")
	}
	if !found {
		return nil, status.Error(codes.NotFound, "Sandbox not found")
	}
	return account, nil
}

func (s *Service) requireKnownPolicySets(ctx context.Context, names []string) error {
	var list policyapi.PolicySetList
	if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
		slog.ErrorContext(ctx, "PolicyService PolicySet read failed", "error", err)
		return status.Error(codes.Unavailable, "PolicySet inventory is unavailable")
	}
	known := make(map[string]struct{}, len(list.Items))
	for _, set := range list.Items {
		known[set.Name] = struct{}{}
	}
	missing := make(map[string]struct{})
	seen := make(map[string]struct{}, len(names))
	for _, name := range names {
		if name == "" {
			return status.Error(codes.InvalidArgument, "PolicySet names must not be empty")
		}
		if _, duplicate := seen[name]; duplicate {
			return status.Error(codes.InvalidArgument, "PolicySet names must be unique")
		}
		seen[name] = struct{}{}
		if _, ok := known[name]; !ok {
			missing[name] = struct{}{}
		}
	}
	if len(missing) == 0 {
		return nil
	}
	unknown := make([]string, 0, len(missing))
	for name := range missing {
		unknown = append(unknown, name)
	}
	sort.Strings(unknown)
	return status.Error(codes.InvalidArgument, fmt.Sprintf("unknown PolicySets: %s", strings.Join(unknown, ", ")))
}

func (s *Service) WatchPolicyBindings(request *policypb.WatchPolicyBindingsRequest, stream grpc.ServerStreamingServer[policypb.WatchPolicyBindingsResponse]) error {
	ctx := stream.Context()
	caller, broad, err := s.caller(ctx)
	if err != nil {
		return err
	}
	selected, err := selectBindings(request, caller, broad)
	if err != nil {
		return err
	}
	bindingWatch, err := s.watch(ctx, &policyapi.PolicyBindingList{})
	if err != nil {
		return err
	}
	defer bindingWatch.Stop()
	read := func() (map[string]*policypb.PolicyBinding, error) {
		var list policyapi.PolicyBindingList
		if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
			return nil, unavailable(ctx, err)
		}
		out := make(map[string]*policypb.PolicyBinding)
		for i := range list.Items {
			binding := &list.Items[i]
			if !selected(binding.Spec.Subject) {
				continue
			}
			converted, err := bindingMessage(binding)
			if err != nil {
				return nil, status.Error(codes.Internal, "invalid policy binding")
			}
			out[binding.Name] = converted
		}
		return out, nil
	}
	return follow(ctx, []watch.Interface{bindingWatch}, read,
		func(items []*policypb.PolicyBinding) error {
			return send(ctx, stream, &policypb.WatchPolicyBindingsResponse{Frame: &policypb.WatchPolicyBindingsResponse_Snapshot{Snapshot: &policypb.PolicyBindingChunk{Bindings: items}}})
		},
		func() error {
			return send(ctx, stream, &policypb.WatchPolicyBindingsResponse{Frame: &policypb.WatchPolicyBindingsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}})
		},
		func(item *policypb.PolicyBinding) error {
			return send(ctx, stream, &policypb.WatchPolicyBindingsResponse{Frame: &policypb.WatchPolicyBindingsResponse_Upsert{Upsert: item}})
		},
		func(id *policypb.PolicyResourceIdentity) error {
			return send(ctx, stream, &policypb.WatchPolicyBindingsResponse{Frame: &policypb.WatchPolicyBindingsResponse_Removed{Removed: id}})
		},
		func(item *policypb.PolicyBinding) *policypb.PolicyResourceIdentity { return item.GetIdentity() })
}

// selectBindings answers which binding subjects a watch request selects, refusing a subject the
// caller may not read.
func selectBindings(request *policypb.WatchPolicyBindingsRequest, caller auth.Account, broad bool) (func(policyapi.Subject) bool, error) {
	switch selector := request.GetSelector().(type) {
	case *policypb.WatchPolicyBindingsRequest_Identity:
		if selector.Identity == "" {
			return nil, status.Error(codes.InvalidArgument, "identity name is required")
		}
		if !broad {
			return nil, status.Error(codes.PermissionDenied, "caller cannot read this subject")
		}
		return func(bound policyapi.Subject) bool {
			return bound.Identity != nil && bound.Identity.Name == selector.Identity
		}, nil
	case *policypb.WatchPolicyBindingsRequest_EveryIdentity:
		if !selector.EveryIdentity {
			return nil, status.Error(codes.InvalidArgument, "every_identity must be true when set")
		}
		if !broad {
			return nil, status.Error(codes.PermissionDenied, "caller cannot read this subject")
		}
		return func(bound policyapi.Subject) bool { return bound.Identity != nil }, nil
	case *policypb.WatchPolicyBindingsRequest_Subject, nil:
		subject := request.GetSubject()
		if subject == nil {
			subject = &identitypb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}
		}
		if subject.GetNamespace() == "" || subject.GetName() == "" {
			return nil, status.Error(codes.InvalidArgument, "subject namespace and name are required")
		}
		if !broad && (caller.Namespace != subject.GetNamespace() || caller.Name != subject.GetName()) {
			return nil, status.Error(codes.PermissionDenied, "caller cannot read this subject")
		}
		return func(bound policyapi.Subject) bool {
			return bound.Identity == nil && bound.Namespace == subject.GetNamespace() && bound.Name == subject.GetName()
		}, nil
	default:
		return nil, status.Error(codes.InvalidArgument, "unknown binding selector")
	}
}

func (s *Service) WatchIdentities(_ *policypb.WatchIdentitiesRequest, stream grpc.ServerStreamingServer[policypb.WatchIdentitiesResponse]) error {
	ctx := stream.Context()
	_, broad, err := s.caller(ctx)
	if err != nil {
		return err
	}
	if !broad {
		return status.Error(codes.PermissionDenied, "caller cannot read identities")
	}
	identities, err := s.watch(ctx, &policyapi.IdentityList{})
	if err != nil {
		return err
	}
	defer identities.Stop()
	read := func() (map[string]*policypb.Identity, error) {
		var list policyapi.IdentityList
		if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
			return nil, unavailable(ctx, err)
		}
		out := make(map[string]*policypb.Identity, len(list.Items))
		for i := range list.Items {
			converted, err := identityMessage(&list.Items[i])
			if err != nil {
				return nil, status.Error(codes.Internal, "invalid identity")
			}
			out[list.Items[i].Name] = converted
		}
		return out, nil
	}
	return follow(ctx, []watch.Interface{identities}, read,
		func(items []*policypb.Identity) error {
			return send(ctx, stream, &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Snapshot{Snapshot: &policypb.IdentityChunk{Identities: items}}})
		},
		func() error {
			return send(ctx, stream, &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}})
		},
		func(item *policypb.Identity) error {
			return send(ctx, stream, &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Upsert{Upsert: item}})
		},
		func(id *policypb.PolicyResourceIdentity) error {
			return send(ctx, stream, &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Removed{Removed: id}})
		},
		func(item *policypb.Identity) *policypb.PolicyResourceIdentity { return item.GetIdentity() })
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
	if _, fromFlux := identity.Labels[fluxNameLabel]; fromFlux {
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
	if len(referencing) > 0 {
		slices.Sort(referencing)
		failure := &errdetails.PreconditionFailure{}
		for _, name := range referencing {
			failure.Violations = append(failure.Violations, &errdetails.PreconditionFailure_Violation{
				Type: "PolicyBinding", Subject: name, Description: "names this Identity and is not owned by it",
			})
		}
		refusal, err := status.New(codes.FailedPrecondition, "Identity is named by PolicyBindings it does not own: "+strings.Join(referencing, ", ")).WithDetails(failure)
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

func ownedBy(owners []metav1.OwnerReference, uid types.UID) bool {
	return slices.ContainsFunc(owners, func(owner metav1.OwnerReference) bool { return owner.UID == uid })
}

func (s *Service) WatchPolicySets(_ *policypb.WatchPolicySetsRequest, stream grpc.ServerStreamingServer[policypb.WatchPolicySetsResponse]) error {
	ctx := stream.Context()
	caller, broad, err := s.caller(ctx)
	if err != nil {
		return err
	}
	bindings, err := s.watch(ctx, &policyapi.PolicyBindingList{})
	if err != nil {
		return err
	}
	defer bindings.Stop()
	sets, err := s.watch(ctx, &policyapi.PolicySetList{})
	if err != nil {
		return err
	}
	defer sets.Stop()
	read := func() (map[string]*policypb.PolicySet, error) {
		allowed := map[string]struct{}{}
		if !broad {
			var list policyapi.PolicyBindingList
			if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
				return nil, unavailable(ctx, err)
			}
			for _, binding := range list.Items {
				if binding.Spec.Subject.Namespace == caller.Namespace && binding.Spec.Subject.Name == caller.Name {
					for _, name := range binding.Spec.PolicySets {
						allowed[name] = struct{}{}
					}
				}
			}
		}
		var list policyapi.PolicySetList
		if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
			return nil, unavailable(ctx, err)
		}
		out := make(map[string]*policypb.PolicySet)
		for i := range list.Items {
			set := &list.Items[i]
			if _, ok := allowed[set.Name]; !broad && !ok {
				continue
			}
			converted, err := policySetMessage(set)
			if err != nil {
				return nil, status.Error(codes.Internal, "invalid policy set")
			}
			out[set.Name] = converted
		}
		return out, nil
	}
	return follow(ctx, []watch.Interface{bindings, sets}, read,
		func(items []*policypb.PolicySet) error {
			return send(ctx, stream, &policypb.WatchPolicySetsResponse{Frame: &policypb.WatchPolicySetsResponse_Snapshot{Snapshot: &policypb.PolicySetChunk{PolicySets: items}}})
		},
		func() error {
			return send(ctx, stream, &policypb.WatchPolicySetsResponse{Frame: &policypb.WatchPolicySetsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}})
		},
		func(item *policypb.PolicySet) error {
			return send(ctx, stream, &policypb.WatchPolicySetsResponse{Frame: &policypb.WatchPolicySetsResponse_Upsert{Upsert: item}})
		},
		func(id *policypb.PolicyResourceIdentity) error {
			return send(ctx, stream, &policypb.WatchPolicySetsResponse{Frame: &policypb.WatchPolicySetsResponse_Removed{Removed: id}})
		},
		func(item *policypb.PolicySet) *policypb.PolicyResourceIdentity { return item.GetIdentity() })
}

// ListPolicySetNames returns one sorted snapshot of the PolicySets visible to the caller.
func (s *Service) ListPolicySetNames(ctx context.Context, _ *policypb.ListPolicySetNamesRequest) (*policypb.ListPolicySetNamesResponse, error) {
	caller, broad, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	allowed := map[string]struct{}{}
	if !broad {
		var bindings policyapi.PolicyBindingList
		if err := s.objects.List(ctx, &bindings, client.InNamespace(s.namespace)); err != nil {
			return nil, unavailable(ctx, err)
		}
		for _, binding := range bindings.Items {
			if binding.Spec.Subject.Namespace == caller.Namespace && binding.Spec.Subject.Name == caller.Name {
				for _, name := range binding.Spec.PolicySets {
					allowed[name] = struct{}{}
				}
			}
		}
	}
	var sets policyapi.PolicySetList
	if err := s.objects.List(ctx, &sets, client.InNamespace(s.namespace)); err != nil {
		return nil, unavailable(ctx, err)
	}
	names := make([]string, 0, len(sets.Items))
	for _, set := range sets.Items {
		if broad {
			names = append(names, set.Name)
			continue
		}
		if _, ok := allowed[set.Name]; ok {
			names = append(names, set.Name)
		}
	}
	slices.Sort(names)
	return &policypb.ListPolicySetNamesResponse{Names: names}, nil
}

func bindingMessage(obj *policyapi.PolicyBinding) (*policypb.PolicyBinding, error) {
	spec := &resourcepb.PolicyBindingSpec{}
	state := &resourcepb.Status{}
	if err := convert(obj.Spec, spec); err != nil {
		return nil, err
	}
	if err := convert(obj.Status, state); err != nil {
		return nil, err
	}
	result := &policypb.PolicyBinding{Identity: identity(obj.ObjectMeta), Generation: obj.Generation, Labels: obj.Labels, Spec: spec, Status: state}
	for _, owner := range obj.OwnerReferences {
		result.OwnerReferences = append(result.OwnerReferences, &kubernetespb.OwnerReference{ApiVersion: owner.APIVersion, Kind: owner.Kind, Name: owner.Name, Uid: string(owner.UID), Controller: owner.Controller != nil && *owner.Controller})
	}
	return result, nil
}

func identityMessage(obj *policyapi.Identity) (*policypb.Identity, error) {
	spec := &resourcepb.IdentitySpec{}
	if err := convert(obj.Spec, spec); err != nil {
		return nil, err
	}
	return &policypb.Identity{Identity: identity(obj.ObjectMeta), Generation: obj.Generation, Labels: obj.Labels, Spec: spec}, nil
}

func policySetMessage(obj *policyapi.PolicySet) (*policypb.PolicySet, error) {
	spec := &resourcepb.PolicySetSpec{}
	if err := convert(obj.Spec, spec); err != nil {
		return nil, err
	}
	return &policypb.PolicySet{Identity: identity(obj.ObjectMeta), Generation: obj.Generation, Spec: spec}, nil
}

func identity(obj metav1.ObjectMeta) *policypb.PolicyResourceIdentity {
	return &policypb.PolicyResourceIdentity{Namespace: obj.Namespace, Name: obj.Name, Uid: string(obj.UID)}
}

func convert(value any, message proto.Message) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return protojson.Unmarshal(data, message)
}

func (s *Service) watch(ctx context.Context, list client.ObjectList) (watch.Interface, error) {
	options := &client.ListOptions{Namespace: s.namespace}
	if err := s.objects.List(ctx, list, options); err != nil {
		return nil, unavailable(ctx, err)
	}
	accessor, ok := list.(metav1.ListInterface)
	if !ok {
		return nil, status.Error(codes.Internal, "invalid list")
	}
	options.Raw = &metav1.ListOptions{ResourceVersion: accessor.GetResourceVersion()}
	result, err := s.objects.Watch(ctx, list, options)
	if err != nil {
		return nil, unavailable(ctx, err)
	}
	return result, nil
}

func unavailable(ctx context.Context, _ error) error {
	if ctx.Err() != nil {
		return status.FromContextError(ctx.Err()).Err()
	}
	return status.Error(codes.Unavailable, "policy watch unavailable; reconnect for a snapshot")
}

func send[T any](ctx context.Context, stream grpc.ServerStreamingServer[T], frame *T) error {
	if err := auth.RevalidateStream(ctx); err != nil {
		return err
	}
	if err := streaming.SendWithin(ctx, writeTimeout, stream.Send, frame); err != nil {
		if errors.Is(err, streaming.ErrSendTimeout) {
			return status.Error(codes.DeadlineExceeded, "policy watch write deadline exceeded")
		}
		return err
	}
	return nil
}

func follow[T proto.Message](ctx context.Context, watches []watch.Interface, read func() (map[string]T, error), snapshot func([]T) error, complete func() error, upsert func(T) error, remove func(*policypb.PolicyResourceIdentity) error, ident func(T) *policypb.PolicyResourceIdentity) error {
	current, err := read()
	if err != nil {
		return err
	}
	keys := make([]string, 0, len(current))
	for key := range current {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	for len(keys) > 0 {
		n := min(len(keys), snapshotChunk)
		items := make([]T, 0, n)
		for _, key := range keys[:n] {
			items = append(items, current[key])
		}
		if err := snapshot(items); err != nil {
			return err
		}
		keys = keys[n:]
	}
	if err := complete(); err != nil {
		return err
	}
	var other <-chan watch.Event
	if len(watches) > 1 {
		other = watches[1].ResultChan()
	}
	ticker := time.NewTicker(recheckInterval)
	defer ticker.Stop()
	for {
		var event watch.Event
		var ok bool
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-ticker.C:
			if err := auth.RevalidateStream(ctx); err != nil {
				return err
			}
			continue
		case event, ok = <-watches[0].ResultChan():
		case event, ok = <-other:
		}
		if !ok {
			return unavailable(ctx, errors.New("watch closed"))
		}
		if event.Type == watch.Error {
			return unavailable(ctx, apierrors.FromObject(event.Object))
		}
		next, err := read()
		if err != nil {
			return err
		}
		removed := make([]string, 0)
		for key, old := range current {
			if item, ok := next[key]; !ok || ident(item).GetUid() != ident(old).GetUid() {
				removed = append(removed, key)
			}
		}
		slices.Sort(removed)
		for _, key := range removed {
			if err := remove(ident(current[key])); err != nil {
				return err
			}
		}
		keys = keys[:0]
		for key := range next {
			keys = append(keys, key)
		}
		slices.Sort(keys)
		for _, key := range keys {
			item := next[key]
			if old, ok := current[key]; !ok || !proto.Equal(old, item) {
				if err := upsert(item); err != nil {
					return err
				}
			}
		}
		current = next
	}
}
