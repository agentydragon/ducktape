package sandboxcontroller

import (
	"context"
	"fmt"
	"reflect"
	"sync"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
)

const runtimeStateTimeout = 30 * time.Second

// RuntimeWatcher observes facts without acquiring the Gateway's admission epoch.
type RuntimeWatcher interface {
	Watch(context.Context, *sandboxapi.Sandbox, *corev1.Pod, string, func(*environmentruntimepb.WatchStateResponse) error) error
}

// RuntimeSnapshot is immutable after publication. LastSeen uses the controller's clock.
type RuntimeSnapshot struct {
	SandboxUID types.UID
	PodUID     string
	BootID     string
	LastSeen   time.Time
	Connected  bool
	Generation uint64
	Facts      EnvironmentRuntimeFacts
}

func (s RuntimeSnapshot) fresh(now time.Time) bool {
	return s.Connected && s.BootID != "" && now.Before(s.LastSeen.Add(runtimeStateTimeout))
}

type runtimeObservation struct {
	sandbox      *sandboxapi.Sandbox
	pod          *corev1.Pod
	credentialID string
	cancel       context.CancelFunc
	snapshot     RuntimeSnapshot
	generation   uint64
}

// RuntimeStates is a leader-only manager runnable. The initial Sandbox list causes
// reconciles on takeover; no facts or idle elapsed time survive a process restart.
type RuntimeStates struct {
	Watcher        RuntimeWatcher
	Now            func() time.Time
	Events         chan event.GenericEvent
	mu             sync.Mutex
	ctx            context.Context
	active         map[types.NamespacedName]*runtimeObservation
	workers        sync.WaitGroup
	nextGeneration uint64
}

func NewRuntimeStates(watcher RuntimeWatcher, now func() time.Time) *RuntimeStates {
	return &RuntimeStates{Watcher: watcher, Now: now, Events: make(chan event.GenericEvent, 1024), active: make(map[types.NamespacedName]*runtimeObservation)}
}

func (*RuntimeStates) NeedLeaderElection() bool { return true }

func (s *RuntimeStates) Start(ctx context.Context) error {
	s.mu.Lock()
	s.ctx = ctx
	for key, observation := range s.active {
		workerCtx, cancel := context.WithCancel(ctx)
		observation.cancel = cancel
		s.workers.Add(1)
		go s.observe(workerCtx, key, observation)
	}
	s.mu.Unlock()
	<-ctx.Done()
	s.mu.Lock()
	for key, observation := range s.active {
		observation.cancel()
		delete(s.active, key)
	}
	s.mu.Unlock()
	s.workers.Wait()
	return nil
}

// Track is called only with the workload incarnation whose credential was accepted.
// Replacing the active pointer fences callbacks even when a cancelled Recv returns late.
func (s *RuntimeStates) Track(sandbox *sandboxapi.Sandbox, pod *corev1.Pod, credentialID string) {
	if s.Watcher == nil {
		return
	}
	key := client.ObjectKeyFromObject(sandbox)
	s.mu.Lock()
	defer s.mu.Unlock()
	current := s.active[key]
	if current != nil && pod != nil && current.sandbox.UID == sandbox.UID && current.pod.UID == pod.UID &&
		current.pod.Status.PodIP == pod.Status.PodIP && current.credentialID == credentialID {
		return
	}
	if current != nil {
		current.cancel()
		delete(s.active, key)
	}
	if pod == nil || pod.Status.PodIP == "" || (s.ctx != nil && s.ctx.Err() != nil) {
		return
	}
	observation := &runtimeObservation{sandbox: sandbox.DeepCopy(), pod: pod.DeepCopy(), credentialID: credentialID, cancel: func() {}}
	// Address and credential changes reset connection/idle evidence, but cannot
	// undo a terminal bootstrap result for the same Sandbox incarnation.
	if current != nil && current.sandbox.UID == sandbox.UID && current.pod.UID == pod.UID &&
		(current.snapshot.Facts.Bootstrap == BootstrapFailed || current.snapshot.Facts.Bootstrap == BootstrapInterrupted) {
		observation.snapshot.Facts.Bootstrap = current.snapshot.Facts.Bootstrap
	}
	s.active[key] = observation
	if s.ctx == nil {
		return
	}
	ctx, cancel := context.WithCancel(s.ctx)
	observation.cancel = cancel
	s.workers.Add(1)
	go s.observe(ctx, key, observation)
}

func (s *RuntimeStates) Forget(key types.NamespacedName) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if current := s.active[key]; current != nil {
		current.cancel()
		delete(s.active, key)
	}
}

func (s *RuntimeStates) Snapshot(sandbox *sandboxapi.Sandbox, podUID string) RuntimeSnapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	current := s.active[client.ObjectKeyFromObject(sandbox)]
	if current == nil || current.snapshot.SandboxUID != sandbox.UID || current.snapshot.PodUID != podUID {
		return RuntimeSnapshot{}
	}
	return current.snapshot
}

func (s *RuntimeStates) notify(ctx context.Context, sandbox *sandboxapi.Sandbox) {
	select {
	case s.Events <- event.GenericEvent{Object: sandbox}:
	case <-ctx.Done():
	}
}

func (s *RuntimeStates) observe(ctx context.Context, key types.NamespacedName, observation *runtimeObservation) {
	defer s.workers.Done()
	for ctx.Err() == nil {
		s.mu.Lock()
		s.nextGeneration++
		generation := s.nextGeneration
		observation.generation = generation
		s.mu.Unlock()
		err := s.Watcher.Watch(ctx, observation.sandbox, observation.pod, observation.credentialID, func(response *environmentruntimepb.WatchStateResponse) error {
			if response.GetSandboxUid() != string(observation.sandbox.UID) || response.GetIncarnationUid() != string(observation.pod.UID) ||
				response.GetEnvironmentRuntimeBootId() == "" || response.GetConnectionEpoch() != 0 || response.GetFacts() == nil {
				return fmt.Errorf("Runtime state has invalid incarnation or facts")
			}
			facts, err := runtimeFactsFromProto(response.Facts)
			if err != nil {
				return err
			}
			s.mu.Lock()
			if s.active[key] != observation || observation.generation != generation || ctx.Err() != nil {
				s.mu.Unlock()
				return context.Canceled
			}
			previous := observation.snapshot
			// Bootstrap failure is terminal for an incarnation, including across a Runtime restart.
			if previous.Facts.Bootstrap == BootstrapFailed || previous.Facts.Bootstrap == BootstrapInterrupted {
				facts.Bootstrap = previous.Facts.Bootstrap
			}
			observation.snapshot = RuntimeSnapshot{
				SandboxUID: observation.sandbox.UID, PodUID: string(observation.pod.UID),
				BootID: response.EnvironmentRuntimeBootId, LastSeen: s.Now(), Connected: true, Facts: facts, Generation: generation,
			}
			changed := !previous.Connected || previous.BootID != response.EnvironmentRuntimeBootId || !reflect.DeepEqual(previous.Facts, facts)
			s.mu.Unlock()
			if changed {
				s.notify(ctx, observation.sandbox)
			}
			return nil
		})
		s.mu.Lock()
		current := s.active[key] == observation
		if current {
			observation.generation = 0
			observation.snapshot.Connected = false
		}
		s.mu.Unlock()
		if !current || ctx.Err() != nil {
			return
		}
		ctrl.LoggerFrom(ctx).Error(err, "Runtime state watch lost", "sandbox", key)
		s.notify(ctx, observation.sandbox)
		timer := time.NewTimer(time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}
