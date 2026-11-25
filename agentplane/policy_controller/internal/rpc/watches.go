package rpc

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	resourcepb "github.com/agentydragon/ducktape/agentplane/policy/api/resourcev1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	kubernetespb "github.com/agentydragon/ducktape/agentplane/proto/kubernetes"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/watch"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func (s *Service) WatchPolicyBindings(request *policypb.WatchPolicyBindingsRequest, stream grpc.ServerStreamingServer[policypb.WatchPolicyBindingsResponse]) error {
	ctx := stream.Context()
	if err := s.requireReader(ctx); err != nil {
		return err
	}
	selected, err := selectBindings(request)
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
	return follow(ctx, bindingWatch, read,
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

// selectBindings requires an explicit subject: a service caller has no account identity.
func selectBindings(request *policypb.WatchPolicyBindingsRequest) (func(policyapi.Subject) bool, error) {
	switch selector := request.GetSelector().(type) {
	case *policypb.WatchPolicyBindingsRequest_Identity:
		if selector.Identity == "" {
			return nil, status.Error(codes.InvalidArgument, "identity name is required")
		}
		return func(bound policyapi.Subject) bool {
			return bound.Identity != nil && bound.Identity.Name == selector.Identity
		}, nil
	case *policypb.WatchPolicyBindingsRequest_EveryIdentity:
		if !selector.EveryIdentity {
			return nil, status.Error(codes.InvalidArgument, "every_identity must be true when set")
		}
		return func(bound policyapi.Subject) bool { return bound.Identity != nil }, nil
	case *policypb.WatchPolicyBindingsRequest_Subject, nil:
		subject := request.GetSubject()
		if subject.GetNamespace() == "" || subject.GetName() == "" {
			return nil, status.Error(codes.InvalidArgument, "subject namespace and name are required")
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
	if err := s.requireReader(ctx); err != nil {
		return err
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
	return follow(ctx, identities, read,
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

func (s *Service) WatchPolicySets(_ *policypb.WatchPolicySetsRequest, stream grpc.ServerStreamingServer[policypb.WatchPolicySetsResponse]) error {
	ctx := stream.Context()
	if err := s.requireReader(ctx); err != nil {
		return err
	}
	sets, err := s.watch(ctx, &policyapi.PolicySetList{})
	if err != nil {
		return err
	}
	defer sets.Stop()
	read := func() (map[string]*policypb.PolicySet, error) {
		var list policyapi.PolicySetList
		if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
			return nil, unavailable(ctx, err)
		}
		out := make(map[string]*policypb.PolicySet)
		for i := range list.Items {
			set := &list.Items[i]
			converted, err := policySetMessage(set)
			if err != nil {
				return nil, status.Error(codes.Internal, "invalid policy set")
			}
			out[set.Name] = converted
		}
		return out, nil
	}
	return follow(ctx, sets, read,
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
	if err := s.requireReader(ctx); err != nil {
		return nil, err
	}
	var sets policyapi.PolicySetList
	if err := s.objects.List(ctx, &sets, client.InNamespace(s.namespace)); err != nil {
		return nil, unavailable(ctx, err)
	}
	names := make([]string, 0, len(sets.Items))
	for _, set := range sets.Items {
		names = append(names, set.Name)
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

func unavailable(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return status.FromContextError(ctx.Err()).Err()
	}
	slog.WarnContext(ctx, "PolicyService watch unavailable", "error", err)
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

func follow[T proto.Message](ctx context.Context, changes watch.Interface, read func() (map[string]T, error), snapshot func([]T) error, complete func() error, upsert func(T) error, remove func(*policypb.PolicyResourceIdentity) error, ident func(T) *policypb.PolicyResourceIdentity) error {
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
		case event, ok = <-changes.ResultChan():
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
