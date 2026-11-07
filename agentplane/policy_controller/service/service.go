// Package service serves Policy resource watches from the Kubernetes API.
package service

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	resourcepb "github.com/agentydragon/ducktape/agentplane/policy/api/resourcev1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	kubernetespb "github.com/agentydragon/ducktape/agentplane/proto/kubernetes"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
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

const (
	recheckInterval = 5 * time.Second
	writeTimeout    = 5 * time.Second
	snapshotChunk   = 64
)

type Service struct {
	policypb.UnimplementedPolicyServiceServer
	namespace string
	objects   client.WithWatch
	readers   map[auth.Account]struct{}
}

func New(namespace string, objects client.WithWatch, readers []auth.Account) (*Service, error) {
	if namespace == "" || objects == nil {
		return nil, errors.New("policy namespace and Kubernetes watch client are required")
	}
	allowed := make(map[auth.Account]struct{}, len(readers))
	for _, account := range readers {
		allowed[account] = struct{}{}
	}
	return &Service{namespace: namespace, objects: objects, readers: allowed}, nil
}

func Methods(resolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccount(resolver)
	return auth.Methods{
		policypb.PolicyService_WatchPolicyBindings_FullMethodName: rule,
		policypb.PolicyService_WatchPolicySets_FullMethodName:     rule,
		policypb.PolicyService_ListPolicySetNames_FullMethodName:  rule,
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

func (s *Service) WatchPolicyBindings(request *policypb.WatchPolicyBindingsRequest, stream grpc.ServerStreamingServer[policypb.WatchPolicyBindingsResponse]) error {
	ctx := stream.Context()
	caller, broad, err := s.caller(ctx)
	if err != nil {
		return err
	}
	subject := request.GetSubject()
	if subject == nil {
		subject = &identitypb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}
	}
	if subject.GetNamespace() == "" || subject.GetName() == "" {
		return status.Error(codes.InvalidArgument, "subject namespace and name are required")
	}
	if !broad && (caller.Namespace != subject.GetNamespace() || caller.Name != subject.GetName()) {
		return status.Error(codes.PermissionDenied, "caller cannot read this subject")
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
			if binding.Spec.Subject.Namespace != subject.GetNamespace() || binding.Spec.Subject.Name != subject.GetName() {
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
