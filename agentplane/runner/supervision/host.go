package supervision

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
)

// Instance is the generic handle for one native execution domain. It
// deliberately contains no resource or backend-specific data.
type Instance struct {
	Done <-chan error
	Stop func(context.Context) error
	PID  int
}

type Launch func(context.Context) (*Instance, error)

type Host struct {
	ctx       context.Context
	state     state.State
	namespace resource.Namespace
	workers   *sync.WaitGroup
	onError   func(error)

	mu      sync.Mutex
	entries map[string]*processHostEntry
	retired map[string]struct{}
}

// Each slot's result is written once before its done channel closes. The live
// instance is retained until stop succeeds; scalar outcome facts remain after
// that handle is released.
type processStartResult struct {
	err     error
	started bool
	pid     int
}

type processStartSlot struct {
	cancel   context.CancelFunc
	done     chan struct{}
	result   processStartResult
	instance *Instance
}

type processExitObservation struct {
	cancel   context.CancelFunc
	done     chan struct{}
	observed bool
	err      error
}

type processStopSlot struct {
	done chan struct{}
	err  error
}

type processHostEntry struct {
	id     string
	spec   DesiredState
	launch Launch

	start *processStartSlot
	exit  *processExitObservation
	stop  *processStopSlot

	revision uint64
}

type processHostSnapshot struct {
	startClaimed bool
	startDone    bool
	startErr     error
	started      bool
	pid          int
	hasInstance  bool

	exitObserved bool
	exitErr      error

	stopClaimed bool
	stopDone    bool
	stopErr     error
	fenced      bool
}

// NewHost binds generic supervision to the caller's state, lifetime and worker barrier.
func NewHost(ctx context.Context, store state.State, namespace resource.Namespace, workers *sync.WaitGroup, onError func(error)) *Host {
	return &Host{
		ctx: ctx, state: store, namespace: namespace, workers: workers, onError: onError,
		entries: make(map[string]*processHostEntry), retired: make(map[string]struct{}),
	}
}

// Controller is registered in the caller's existing COSI runtime.
func (h *Host) Controller() controller.QController { return &Controller{host: h} }

// Request records the callback before publishing desired state. The callback
// is immutable for an ID, even if a controller retries with a fresh closure.
func (h *Host) Request(ctx context.Context, r controller.QRuntime, id string, spec DesiredState, launch Launch) error {
	if r == nil {
		return errors.New("process desired-state writer is not configured")
	}
	return h.register(ctx, r, id, spec, launch, func() error {
		_, err := writeResource(ctx, r, h.namespace, id, spec)
		return err
	})
}

// Native protocol/setup workers publish their intent through the host boundary.
// They wait for the same controller as callers which publish from reconciliation.
func (h *Host) requestNative(ctx context.Context, id string, spec DesiredState, launch Launch) error {
	return h.register(ctx, h.state, id, spec, launch, func() error {
		return publishResource(ctx, h.state, h.namespace, id, spec)
	})
}

func (h *Host) register(ctx context.Context, reader controller.Reader, id string, spec DesiredState, launch Launch, publish func() error) error {
	if h == nil {
		return errors.New("process host is not configured")
	}
	if id == "" {
		return errors.New("process ID is required")
	}
	if spec.Owner == "" {
		return errors.New("process owner is required")
	}
	if launch == nil {
		return errors.New("process launch callback is required")
	}

	h.mu.Lock()
	defer h.mu.Unlock()
	if _, retired := h.retired[id]; retired {
		return fmt.Errorf("process ID %q has already been retired", id)
	}
	entry := h.entries[id]
	newEntry := entry == nil
	if newEntry {
		entry = &processHostEntry{id: id, spec: spec, launch: launch}
		h.entries[id] = entry
	} else if entry.spec != spec {
		return fmt.Errorf("process %q desired state is immutable", id)
	}

	current, found, err := readResource[DesiredState](ctx, reader, h.namespace, id)
	if err != nil {
		if newEntry {
			delete(h.entries, id)
		}
		return err
	}
	if found {
		if current != spec {
			if newEntry {
				delete(h.entries, id)
			}
			return fmt.Errorf("process %q desired state is immutable", id)
		}
		return nil
	}
	if !newEntry {
		// An entry with a previously published intent must not recreate an ID
		// after its owner has destroyed that intent.
		return fmt.Errorf("process %q desired state was already destroyed", id)
	}

	if err := publish(); err != nil {
		delete(h.entries, id)
		return err
	}
	return nil
}

func (h *Host) snapshot(id string) (processHostSnapshot, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	entry := h.entries[id]
	if entry == nil {
		return processHostSnapshot{}, false
	}

	var snapshot processHostSnapshot
	if start := entry.start; start != nil {
		snapshot.startClaimed = true
		snapshot.startDone = processSlotDone(start.done)
		snapshot.startErr = start.result.err
		snapshot.started = start.result.started
		snapshot.pid = start.result.pid
		snapshot.hasInstance = start.instance != nil
		if snapshot.startDone && !snapshot.started && !snapshot.hasInstance {
			// The callback either never ran or returned no instance and owns its
			// cleanup on error, so there is no native domain to fence.
			snapshot.fenced = true
		}
	}
	if exit := entry.exit; exit != nil {
		snapshot.exitObserved = exit.observed
		snapshot.exitErr = exit.err
	}
	if stop := entry.stop; stop != nil {
		snapshot.stopClaimed = true
		snapshot.stopDone = processSlotDone(stop.done)
		snapshot.stopErr = stop.err
		if snapshot.stopDone && stop.err == nil {
			snapshot.fenced = true
		}
	}

	return snapshot, true
}

func (h *Host) start(id string) error {
	h.mu.Lock()
	entry := h.entries[id]
	if entry == nil {
		h.mu.Unlock()
		return fmt.Errorf("process %q has no registered launch callback", id)
	}
	if entry.start != nil {
		h.mu.Unlock()
		return nil
	}
	start := &processStartSlot{done: make(chan struct{})}
	entry.start = start
	h.workers.Add(1)
	h.mu.Unlock()

	go h.runStart(entry, start)
	return nil
}

func (h *Host) runStart(entry *processHostEntry, start *processStartSlot) {
	defer h.workers.Done()

	var ctx context.Context
	var cancel context.CancelFunc
	if entry.spec.StartDeadline.IsZero() {
		ctx, cancel = context.WithCancel(h.ctx)
	} else {
		ctx, cancel = context.WithDeadline(h.ctx, entry.spec.StartDeadline)
	}

	h.mu.Lock()
	if h.entries[entry.id] != entry {
		h.mu.Unlock()
		cancel()
		return
	}
	start.cancel = cancel
	stopAlreadyRequested := entry.stop != nil
	h.mu.Unlock()
	if stopAlreadyRequested {
		cancel()
	}

	instance, startErr := entry.launch(ctx)
	launchContextErr := ctx.Err()
	cancel()
	if startErr == nil && launchContextErr != nil {
		startErr = launchContextErr
	}
	if instance == nil && startErr == nil {
		startErr = errors.New("process launch returned no instance")
	}

	h.mu.Lock()
	if h.entries[entry.id] != entry {
		h.mu.Unlock()
		// Retirement cannot normally pass an unfinished start slot. Keep a
		// defensive fence here in case a future caller violates that contract.
		if instance != nil && instance.Stop != nil {
			stopCtx, stopCancel := context.WithTimeout(h.ctx, entry.spec.StopTimeout)
			_ = instance.Stop(stopCtx)
			stopCancel()
		}
		return
	}
	entry.launch = nil
	start.cancel = nil
	start.result = processStartResult{err: startErr}
	if instance != nil {
		start.result.started = true
		start.result.pid = instance.PID
		start.instance = instance
		h.startObserverLocked(entry, instance)
	}

	// Closing the result slot while holding h.mu makes its result visible before
	// the following input event can wake Reconcile.
	close(start.done)
	publishErr := h.publishInputLocked(entry)
	stopForUnusableStart := instance != nil && startErr != nil || launchContextErr != nil
	instance = nil // the slot or observer now owns any live instance handle.
	h.mu.Unlock()
	h.notify(publishErr)

	if stopForUnusableStart {
		_ = h.requestStop(entry.id)
	}
}

func (h *Host) startObserverLocked(entry *processHostEntry, instance *Instance) {
	if entry.exit != nil {
		return
	}
	ctx, cancel := context.WithCancel(h.ctx)
	exit := &processExitObservation{cancel: cancel, done: make(chan struct{})}
	entry.exit = exit
	h.workers.Add(1)
	go h.observe(ctx, entry, instance, exit)
}

func (h *Host) observe(ctx context.Context, entry *processHostEntry, instance *Instance, exit *processExitObservation) {
	defer h.workers.Done()

	var exitErr error
	observed := false
	select {
	case exitErr, _ = <-instance.Done:
		observed = true
	case <-ctx.Done():
	}
	instance = nil

	h.mu.Lock()
	if h.entries[entry.id] == entry && entry.exit == exit && observed {
		exit.observed = true
		exit.err = exitErr
		publishErr := h.publishInputLocked(entry)
		h.mu.Unlock()
		h.notify(publishErr)
		close(exit.done)
		return
	}
	h.mu.Unlock()
	close(exit.done)
}

// requestStop installs the only stop slot and cancels a launch that is still
// resolving. The stop worker joins the launch before fencing a late instance.
func (h *Host) requestStop(id string) error {
	h.mu.Lock()
	entry := h.entries[id]
	if entry == nil {
		h.mu.Unlock()
		return nil
	}
	if entry.stop != nil {
		h.mu.Unlock()
		return nil
	}
	start := entry.start
	if start == nil {
		start = &processStartSlot{done: make(chan struct{})}
		close(start.done)
		entry.start = start
	}
	stop := &processStopSlot{done: make(chan struct{})}
	entry.stop = stop
	startCancel := start.cancel
	h.workers.Add(1)
	h.mu.Unlock()

	if startCancel != nil {
		startCancel()
	}
	go h.runStop(entry, start, stop)
	return nil
}

func (h *Host) runStop(entry *processHostEntry, start *processStartSlot, stop *processStopSlot) {
	defer h.workers.Done()

	<-start.done

	h.mu.Lock()
	var instance *Instance
	if h.entries[entry.id] == entry && entry.start == start {
		instance = start.instance
	}
	h.mu.Unlock()

	var stopErr error
	if instance != nil {
		stopFn := instance.Stop
		if stopFn == nil {
			stopErr = errors.New("process instance has no stop callback")
		} else {
			ctx, cancel := context.WithTimeout(h.ctx, entry.spec.StopTimeout)
			stopErr = stopFn(ctx)
			cancel()
		}
		stopFn = nil
	}

	if stopErr == nil && instance != nil {
		h.mu.Lock()
		var exit *processExitObservation
		if h.entries[entry.id] == entry {
			exit = entry.exit
		}
		h.mu.Unlock()
		if exit != nil {
			exit.cancel()
			<-exit.done
		}
	}
	instance = nil

	h.mu.Lock()
	if h.entries[entry.id] != entry {
		h.mu.Unlock()
		return
	}
	if stopErr == nil {
		start.instance = nil
	}
	stop.err = stopErr
	// The finalizer may be released only after the completed fence is visible.
	close(stop.done)
	publishErr := h.publishInputLocked(entry)
	h.mu.Unlock()
	h.notify(publishErr)
}

// retire removes callbacks and handles only after every operation and exit
// observation is settled. A small ID marker prevents a stale owner retry from
// replaying a destroyed identity.
func (h *Host) retire(ctx context.Context, r controller.QRuntime, id string) (bool, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, found, err := readResource[DesiredState](ctx, r, h.namespace, id); err != nil {
		return false, err
	} else if found {
		return false, nil
	}
	entry := h.entries[id]
	if entry != nil && !processHostEntrySafeToRetire(entry) {
		return false, nil
	}
	if entry != nil {
		delete(h.entries, id)
	}
	h.retired[id] = struct{}{}
	return true, nil
}

func processHostEntrySafeToRetire(entry *processHostEntry) bool {
	if entry.start != nil {
		if !processSlotDone(entry.start.done) || entry.start.instance != nil {
			return false
		}
	}
	if entry.stop != nil {
		if !processSlotDone(entry.stop.done) || entry.stop.err != nil {
			return false
		}
	}
	if entry.exit != nil && !processSlotDone(entry.exit.done) {
		return false
	}
	return true
}

func processSlotDone(done <-chan struct{}) bool {
	if done == nil {
		return false
	}
	select {
	case <-done:
		return true
	default:
		return false
	}
}

func (h *Host) publishInputLocked(entry *processHostEntry) error {
	if h.entries[entry.id] != entry {
		return nil
	}
	entry.revision++
	return publishResource(h.ctx, h.state, h.namespace, entry.id, inputSpec{Revision: entry.revision})
}

func (h *Host) notify(err error) {
	if err != nil && !errors.Is(err, context.Canceled) {
		h.onError(fmt.Errorf("publish process lifecycle input: %w", err))
	}
}
