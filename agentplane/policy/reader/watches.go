package reader

import (
	"context"
	"fmt"
	"net/url"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
)

// StaleAfterCycles is how many watch periods a kind may miss before the view counts as stale: one
// late cycle is a slow API server, three in a row is a wedge. A reader builds its Freshness with
// StaleAfterCycles of the period it gives its Watches.
const StaleAfterCycles = 3

// MinWatchPeriod is the shortest period client-go does not take for a failing watch: it relists after
// one that closes in under a second without an event.
const MinWatchPeriod = 2 * time.Second

// Watches runs one reader's shared informers. Each lists and watches through a client whose watches
// the server ends after one period, so a watch held to its end records a completed cycle of its
// kind in the reader's Freshness.
//
// Each kind is a client-go shared informer over a typed client, so the list, watch, resume and
// backoff are client-go's.
type Watches struct {
	freshness *Freshness
	base      *rest.Config
	config    *rest.Config
	period    int64
	informers []cache.SharedIndexInformer
	synced    []cache.InformerSynced
	// afterSync runs once every kind has listed and its objects are applied.
	afterSync []func(context.Context)
	// shutdown runs when Run returns, whether or not the first sync completed.
	shutdown []func()
}

// NewWatches returns Watches that are idle until Run. The period is a whole number of seconds, at
// least MinWatchPeriod.
func NewWatches(base *rest.Config, freshness *Freshness, period time.Duration) (*Watches, error) {
	if period < MinWatchPeriod || period%time.Second != 0 {
		return nil, fmt.Errorf("the watch period is a whole number of seconds, at least %v: %v", MinWatchPeriod, period)
	}
	w := &Watches{freshness: freshness, base: base, period: int64(period / time.Second)}
	w.config = w.recording(kindByPath)
	return w, nil
}

// Config is the client configuration every watched kind is read through, so its cycles are recorded,
// each as the kind its path names.
func (w *Watches) Config() *rest.Config { return w.config }

// ConfigRecordingAs is a client configuration that records its watches' cycles as kind: for a resource
// watched in several namespaces, whose path alone does not tell the namespaces' cycles apart.
func (w *Watches) ConfigRecordingAs(kind Kind) *rest.Config {
	return w.recording(func(*url.URL) Kind { return kind })
}

func (w *Watches) recording(kindOf func(*url.URL) Kind) *rest.Config {
	recorded := rest.CopyConfig(w.base)
	recorded.Wrap(cycleRecorder{freshness: w.freshness, now: time.Now, kindOf: kindOf}.wrap)
	return recorded
}

// TweakListOptions is every informer's list-options tweak: the server ends each watch after one
// period, so a watch it ends on schedule stands as proof of life.
func (w *Watches) TweakListOptions(options *metav1.ListOptions) { options.TimeoutSeconds = &w.period }

// Object is what a typed shared informer holds: a pointer to a resource.
type Object interface {
	cache.Object
	runtime.Object
}

// Watch applies a generated shared informer's events through put and remove. Typed handlers
// preserve tombstone deletion and registration-level synchronization. The informer must be built
// over a client from Config with TweakListOptions.
func Watch[T Object](w *Watches, informer cache.TypedSharedIndexInformer[T], put func(T), remove func(name string)) error {
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

// Run lists and watches until ctx is done. The Freshness is marked synced once every kind has listed
// and its objects are applied.
func (w *Watches) Run(ctx context.Context) {
	started := time.Now()
	for _, kind := range w.freshness.kinds {
		w.freshness.RecordCycle(kind, started)
	}
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
	w.freshness.MarkSynced()
	for _, start := range w.afterSync {
		group.StartWithContext(ctx, start)
	}
	<-ctx.Done()
}
