// Package reader reads PolicySets for a Go service that enforces one rule type: an Index of that
// type's rules and the bindings that grant them, kept equal to the API server by shared informers
// whose Health says when it may be acted on, and the service's condition on each PolicyBinding.
package reader

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
)

const (
	PolicySets     watchhealth.Kind = "policysets"
	PolicyBindings watchhealth.Kind = "policybindings"
)

// Watches runs one reader's shared informers and keeps its Health. Each kind is a client-go shared
// informer over a typed client, so the list, watch, timeouts, resume and backoff are client-go's.
type Watches struct {
	health    *watchhealth.Health
	base      *rest.Config
	config    *rest.Config
	informers []cache.SharedIndexInformer
	synced    []cache.InformerSynced
	// afterSync runs once every kind has listed and its objects are applied.
	afterSync []func(context.Context)
	// shutdown runs when Run returns, whether or not the first sync completed.
	shutdown []func()
}

// NewWatches returns Watches that are idle until Run.
func NewWatches(base *rest.Config, health *watchhealth.Health) *Watches {
	return &Watches{health: health, base: base, config: health.Config(base, watchhealth.KindByPath)}
}

// Config is the client configuration every watched kind is read through, so each request records
// the health of the kind its path names.
func (w *Watches) Config() *rest.Config { return w.config }

// ConfigRecordingAs is a client configuration that records its requests as kind: for a resource
// watched in several namespaces, whose path alone does not tell the namespaces apart.
func (w *Watches) ConfigRecordingAs(kind watchhealth.Kind) *rest.Config {
	return w.health.Config(w.base, watchhealth.RecordingAs(kind))
}

// Object is what a typed shared informer holds: a pointer to a resource.
type Object interface {
	cache.Object
	runtime.Object
}

// Watch applies a generated shared informer's events through put and remove, and records the errors
// that end its ListAndWatch as kind's. Typed handlers preserve tombstone deletion and
// registration-level synchronization. The informer must be built over a client from Config or
// ConfigRecordingAs(kind).
func Watch[T Object](w *Watches, kind watchhealth.Kind, informer cache.TypedSharedIndexInformer[T], put func(T), remove func(name string)) error {
	if err := informer.SetWatchErrorHandlerWithContext(w.health.WatchErrorHandler(kind)); err != nil {
		return err
	}
	registration, err := informer.AddTypedEventHandler(cache.TypedResourceEventHandlerFuncs[T]{
		AddFunc:    put,
		UpdateFunc: func(_, updated T) { put(updated) },
		DeleteFunc: func(deleted cache.DeletedObject[T]) { remove(deleted.GetName()) },
	})
	if err != nil {
		return err
	}
	w.informers = append(w.informers, informer)
	w.synced = append(w.synced, registration.HasSynced)
	return nil
}

// Run lists and watches until ctx is done. The Health is marked synced once every kind has listed
// and its objects are applied.
func (w *Watches) Run(ctx context.Context) {
	var group wait.Group
	defer group.Wait()
	for _, shutdown := range w.shutdown {
		defer shutdown()
	}
	for _, informer := range w.informers {
		group.StartWithContext(ctx, informer.RunWithContext)
	}
	if !cache.WaitForCacheSync(ctx.Done(), w.synced...) {
		return
	}
	w.health.MarkSynced()
	for _, start := range w.afterSync {
		group.StartWithContext(ctx, start)
	}
	<-ctx.Done()
}
