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
	"google.golang.org/protobuf/types/known/timestamppb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/watch"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func (s *Service) WatchPolicyBindings(request *policypb.WatchPolicyBindingsRequest, stream grpc.ServerStreamingServer[policypb.WatchPolicyBindingsResponse]) error {
	if err := s.requireReader(stream.Context()); err != nil {
		return err
	}
	selected, err := selectBindings(request)
	if err != nil {
		return err
	}
	return serveWatch(s, stream, func() *policyapi.PolicyBindingList { return &policyapi.PolicyBindingList{} },
		func(list *policyapi.PolicyBindingList) (map[string]*policypb.PolicyBinding, error) {
			return messages(list.Items, func(binding *policyapi.PolicyBinding) bool { return selected(binding.Spec.Subject) }, bindingMessage, "invalid policy binding")
		},
		watchFrames[*policypb.PolicyBinding, policypb.WatchPolicyBindingsResponse]{
			snapshot: func(items []*policypb.PolicyBinding) *policypb.WatchPolicyBindingsResponse {
				return &policypb.WatchPolicyBindingsResponse{Frame: &policypb.WatchPolicyBindingsResponse_Snapshot{Snapshot: &policypb.PolicyBindingChunk{Bindings: items}}}
			},
			complete: func() *policypb.WatchPolicyBindingsResponse {
				return &policypb.WatchPolicyBindingsResponse{Frame: &policypb.WatchPolicyBindingsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}}
			},
			upsert: func(item *policypb.PolicyBinding) *policypb.WatchPolicyBindingsResponse {
				return &policypb.WatchPolicyBindingsResponse{Frame: &policypb.WatchPolicyBindingsResponse_Upsert{Upsert: item}}
			},
			removed: func(id *policypb.PolicyResourceIdentity) *policypb.WatchPolicyBindingsResponse {
				return &policypb.WatchPolicyBindingsResponse{Frame: &policypb.WatchPolicyBindingsResponse_Removed{Removed: id}}
			},
		})
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
	if err := s.requireReader(stream.Context()); err != nil {
		return err
	}
	return serveWatch(s, stream, func() *policyapi.IdentityList { return &policyapi.IdentityList{} },
		func(list *policyapi.IdentityList) (map[string]*policypb.Identity, error) {
			return messages(list.Items, every, identityMessage, "invalid identity")
		},
		watchFrames[*policypb.Identity, policypb.WatchIdentitiesResponse]{
			snapshot: func(items []*policypb.Identity) *policypb.WatchIdentitiesResponse {
				return &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Snapshot{Snapshot: &policypb.IdentityChunk{Identities: items}}}
			},
			complete: func() *policypb.WatchIdentitiesResponse {
				return &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}}
			},
			upsert: func(item *policypb.Identity) *policypb.WatchIdentitiesResponse {
				return &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Upsert{Upsert: item}}
			},
			removed: func(id *policypb.PolicyResourceIdentity) *policypb.WatchIdentitiesResponse {
				return &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Removed{Removed: id}}
			},
		})
}

func (s *Service) WatchPolicySets(_ *policypb.WatchPolicySetsRequest, stream grpc.ServerStreamingServer[policypb.WatchPolicySetsResponse]) error {
	if err := s.requireReader(stream.Context()); err != nil {
		return err
	}
	return serveWatch(s, stream, func() *policyapi.PolicySetList { return &policyapi.PolicySetList{} },
		func(list *policyapi.PolicySetList) (map[string]*policypb.PolicySet, error) {
			return messages(list.Items, every, policySetMessage, "invalid policy set")
		},
		watchFrames[*policypb.PolicySet, policypb.WatchPolicySetsResponse]{
			snapshot: func(items []*policypb.PolicySet) *policypb.WatchPolicySetsResponse {
				return &policypb.WatchPolicySetsResponse{Frame: &policypb.WatchPolicySetsResponse_Snapshot{Snapshot: &policypb.PolicySetChunk{PolicySets: items}}}
			},
			complete: func() *policypb.WatchPolicySetsResponse {
				return &policypb.WatchPolicySetsResponse{Frame: &policypb.WatchPolicySetsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}}
			},
			upsert: func(item *policypb.PolicySet) *policypb.WatchPolicySetsResponse {
				return &policypb.WatchPolicySetsResponse{Frame: &policypb.WatchPolicySetsResponse_Upsert{Upsert: item}}
			},
			removed: func(id *policypb.PolicyResourceIdentity) *policypb.WatchPolicySetsResponse {
				return &policypb.WatchPolicySetsResponse{Frame: &policypb.WatchPolicySetsResponse_Removed{Removed: id}}
			},
		})
}

// watchFrames builds one Watch RPC's response frames.
type watchFrames[T, F any] struct {
	snapshot func([]T) *F
	complete func() *F
	upsert   func(T) *F
	removed  func(*policypb.PolicyResourceIdentity) *F
}

// policyResource is a policy object's wire message.
type policyResource interface {
	proto.Message
	GetIdentity() *policypb.PolicyResourceIdentity
}

// serveWatch streams the policy namespace's objects of one kind: a snapshot, then upserts and removals as they change.
// read converts one fresh List.
func serveWatch[L client.ObjectList, T policyResource, F any](s *Service, stream grpc.ServerStreamingServer[F], newList func() L, read func(L) (map[string]T, error), frames watchFrames[T, F]) error {
	ctx := stream.Context()
	changes, err := s.watch(ctx, newList())
	if err != nil {
		return err
	}
	defer changes.Stop()
	return follow(ctx, changes,
		func() (map[string]T, error) {
			list := newList()
			if err := s.objects.List(ctx, list, client.InNamespace(s.namespace)); err != nil {
				return nil, unavailable(ctx, err)
			}
			return read(list)
		},
		func(frame *F) error { return send(ctx, stream, frame) },
		frames)
}

// messages converts the listed objects keep admits, keyed by name; invalid names the Internal error for one that
// does not convert.
func messages[O any, PO interface {
	*O
	GetName() string
}, T any](items []O, keep func(PO) bool, convert func(PO) (T, error), invalid string) (map[string]T, error) {
	out := make(map[string]T)
	for i := range items {
		item := PO(&items[i])
		if !keep(item) {
			continue
		}
		converted, err := convert(item)
		if err != nil {
			return nil, status.Error(codes.Internal, invalid)
		}
		out[item.GetName()] = converted
	}
	return out, nil
}

func every[O any](O) bool { return true }

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
	message := &policypb.Identity{Identity: identity(obj.ObjectMeta), Generation: obj.Generation, Labels: obj.Labels, Spec: spec}
	if obj.DeletionTimestamp != nil {
		message.DeletionTimestamp = timestamppb.New(obj.DeletionTimestamp.Time)
	}
	return message, nil
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

func follow[T policyResource, F any](ctx context.Context, changes watch.Interface, read func() (map[string]T, error), emit func(*F) error, frames watchFrames[T, F]) error {
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
		if err := emit(frames.snapshot(items)); err != nil {
			return err
		}
		keys = keys[n:]
	}
	if err := emit(frames.complete()); err != nil {
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
			if item, ok := next[key]; !ok || item.GetIdentity().GetUid() != old.GetIdentity().GetUid() {
				removed = append(removed, key)
			}
		}
		slices.Sort(removed)
		for _, key := range removed {
			if err := emit(frames.removed(current[key].GetIdentity())); err != nil {
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
				if err := emit(frames.upsert(item)); err != nil {
					return err
				}
			}
		}
		current = next
	}
}
