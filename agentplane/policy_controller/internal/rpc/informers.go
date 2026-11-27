package rpc

import (
	"context"
	"log/slog"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	toolscache "k8s.io/client-go/tools/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// informers are the policy namespace's shared informers, one per watched kind. Every Watch stream of a kind reads its
// store and wakes on its events, so a change costs each stream one diff against memory rather than an API List.
type informers struct {
	bindings, identities, sets toolscache.SharedIndexInformer
}

func newInformers(objects client.WithWatch, namespace string) (informers, error) {
	out := informers{
		bindings:   newInformer(objects, namespace, &policyapi.PolicyBinding{}, func() client.ObjectList { return &policyapi.PolicyBindingList{} }),
		identities: newInformer(objects, namespace, &policyapi.Identity{}, func() client.ObjectList { return &policyapi.IdentityList{} }),
		sets:       newInformer(objects, namespace, &policyapi.PolicySet{}, func() client.ObjectList { return &policyapi.PolicySetList{} }),
	}
	for kind, informer := range map[string]toolscache.SharedIndexInformer{"PolicyBinding": out.bindings, "Identity": out.identities, "PolicySet": out.sets} {
		// The reflector relists and rewatches by itself; streams keep serving the last synced state meanwhile.
		if err := informer.SetWatchErrorHandlerWithContext(func(ctx context.Context, _ *toolscache.Reflector, err error) {
			slog.WarnContext(ctx, "PolicyService informer watch failed", "kind", kind, "error", err)
		}); err != nil {
			return informers{}, err
		}
	}
	return out, nil
}

func newInformer(objects client.WithWatch, namespace string, object client.Object, newList func() client.ObjectList) toolscache.SharedIndexInformer {
	options := func(raw metav1.ListOptions) *client.ListOptions {
		return &client.ListOptions{Namespace: namespace, Raw: &raw}
	}
	return toolscache.NewSharedIndexInformer(listWatch{&toolscache.ListWatch{
		ListWithContextFunc: func(ctx context.Context, raw metav1.ListOptions) (runtime.Object, error) {
			list := newList()
			if err := objects.List(ctx, list, options(raw)); err != nil {
				return nil, err
			}
			return list, nil
		},
		WatchFuncWithContext: func(ctx context.Context, raw metav1.ListOptions) (watch.Interface, error) {
			return objects.Watch(ctx, newList(), options(raw))
		},
	}}, object, 0, toolscache.Indexers{})
}

// listWatch opts out of WatchList streaming: the controller-runtime fake client that tests pass as objects does not
// send its initial-events bookmark, and a classic List then Watch costs one List per informer start.
type listWatch struct{ *toolscache.ListWatch }

func (listWatch) IsWatchListSemanticsUnSupported() bool { return true }

// run runs the informers until ctx ends.
func (i informers) run(ctx context.Context) {
	done := make(chan struct{})
	for _, informer := range []toolscache.SharedIndexInformer{i.bindings, i.identities, i.sets} {
		go func() { informer.RunWithContext(ctx); done <- struct{}{} }()
	}
	for range 3 {
		<-done
	}
}
