package dispatcher

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"k8s.io/client-go/util/workqueue"
)

type HostActivityCatalog interface {
	SandboxCatalog
	EnvironmentReader
}

type HostActivityClient interface {
	WatchActivity(context.Context, SandboxSnapshot, func(*harnesshostpb.WatchSessionsResponse) error) error
}

type SamplerActivityClient interface {
	WatchActivity(context.Context, func(*harnesshostpb.WatchSessionsResponse) error) error
}

// HostActivityObserver owns per-Sandbox watches independently of per-Session command delivery.
type HostActivityObserver struct {
	Store     *store.Store
	Catalog   HostActivityCatalog
	Client    HostActivityClient
	Sampler   SamplerActivityClient
	Hub       *wakeups.Hub
	Namespace string
}

type activityRun struct {
	down   dispatcherpb.SessionActivity_EnvironmentDown_Cause
	owner  store.ActivityOwner
	cancel context.CancelFunc
	done   chan struct{}
}

func (o *HostActivityObserver) Run(ctx context.Context) error {
	if o.Store == nil || o.Catalog == nil || o.Client == nil || o.Hub == nil || o.Namespace == "" {
		return errors.New("host activity observer dependencies are required")
	}
	ctx, cancel := context.WithCancel(ctx)
	queue := workqueue.NewTypedRateLimitingQueue(workqueue.NewTypedItemExponentialFailureRateLimiter[SandboxKey](500*time.Millisecond, 30*time.Second))
	var workers sync.WaitGroup
	defer func() { cancel(); queue.ShutDown(); workers.Wait() }()
	if o.Sampler != nil {
		workers.Go(func() { o.observeSampler(ctx) })
	}
	changed, unsubscribe, err := o.Hub.SubscribeKey(store.SessionNotifyChannel, "all")
	if err != nil {
		return err
	}
	defer unsubscribe()
	stop, err := o.Catalog.WatchSandboxes(func(key SandboxKey) { queue.Add(key) })
	if err != nil {
		return err
	}
	defer stop()
	if !o.Catalog.WaitSynced(ctx) {
		if err := ctx.Err(); err != nil {
			return err
		}
		return errors.New("Sandbox inventory did not synchronize")
	}
	// This map belongs to the sole queue worker. Stream completion only enqueues its key.
	active := map[SandboxKey]*activityRun{}
	workers.Go(func() {
		defer func() {
			for _, run := range active {
				run.cancel()
			}
			for _, run := range active {
				<-run.done
			}
		}()
		for {
			key, shutdown := queue.Get()
			if shutdown {
				return
			}
			func() {
				defer queue.Done(key)
				if ctx.Err() != nil {
					return
				}
				if key.UID == "" {
					return
				}
				hosted, err := o.Store.HasHostedActivity(ctx, key.UID)
				if err != nil {
					queue.AddRateLimited(key)
					return
				}
				if !hosted {
					queue.Forget(key)
					return
				}
				down, snapshot, err := o.environmentState(ctx, key)
				if err != nil {
					slog.Warn("read activity Sandbox", "sandbox_uid", key.UID, "error", err)
					queue.AddRateLimited(key)
					return
				}
				run := active[key]
				if run != nil {
					select {
					case <-run.done:
						delete(active, key)
						run = nil
					default:
					}
				}
				if run != nil {
					if run.down == down && (down != dispatcherpb.SessionActivity_EnvironmentDown_CAUSE_UNSPECIFIED || snapshot.Blocker == "") {
						if down == dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED {
							// Sessions created while suspended also need a confirmed down record.
							if err := o.Store.RecordEnvironmentDown(ctx, run.owner, func(ctx context.Context) (dispatcherpb.SessionActivity_EnvironmentDown_Cause, error) {
								current, _, err := o.environmentState(ctx, key)
								return current, err
							}); err != nil {
								queue.AddRateLimited(key)
								return
							}
						}
						queue.Forget(key)
						return
					}
					run.cancel()
					<-run.done
					delete(active, key)
				}
				owner, err := o.Store.AcquireActivity(ctx, key.UID)
				if err != nil {
					var held *lease.HeldError
					if errors.As(err, &held) {
						queue.AddAfter(key, time.Until(held.Until))
						queue.Forget(key)
						return
					}
					slog.Warn("acquire activity lease", "sandbox_uid", key.UID, "error", err)
					queue.AddRateLimited(key)
					return
				}
				owner, err = o.Store.BeginActivityEpoch(ctx, owner, down == dispatcherpb.SessionActivity_EnvironmentDown_CAUSE_UNSPECIFIED)
				if err != nil {
					o.release(ctx, owner)
					queue.AddRateLimited(key)
					return
				}
				if down != dispatcherpb.SessionActivity_EnvironmentDown_CAUSE_UNSPECIFIED {
					err = o.Store.RecordEnvironmentDown(ctx, owner, func(ctx context.Context) (dispatcherpb.SessionActivity_EnvironmentDown_Cause, error) {
						current, _, err := o.environmentState(ctx, key)
						return current, err
					})
					if err != nil || down == dispatcherpb.SessionActivity_EnvironmentDown_DELETED {
						o.release(ctx, owner)
						if err != nil {
							queue.AddRateLimited(key)
						} else {
							queue.Forget(key)
						}
						return
					}
				}
				if down == dispatcherpb.SessionActivity_EnvironmentDown_CAUSE_UNSPECIFIED && snapshot.Blocker != "" {
					// Unready or unreachable is not evidence of suspension or idleness.
					_ = o.Store.LoseActivityStream(ctx, owner)
					o.release(ctx, owner)
					queue.AddRateLimited(key)
					return
				}
				watchCtx, end := context.WithCancel(ctx)
				run = &activityRun{cancel: end, done: make(chan struct{}), down: down, owner: owner}
				active[key] = run
				workers.Go(func() {
					defer close(run.done)
					defer queue.AddRateLimited(key)
					defer o.release(context.WithoutCancel(ctx), owner)
					o.observe(watchCtx, owner, snapshot, down, func(ctx context.Context, apply func(*harnesshostpb.WatchSessionsResponse) error) error {
						return o.Client.WatchActivity(ctx, snapshot, apply)
					})
				})
				queue.Forget(key)
			}()
		}
	})
	// Notifications are subscribed before the initial scan; reconnect rescans committed rows.
	var last int64
	generation := o.Hub.Generation()
	scan := func() error {
		keys, high, err := o.Store.ActivitySandboxes(ctx, last)
		if err != nil {
			return err
		}
		for key := range keys {
			queue.Add(SandboxKey{Name: key.Name, UID: key.UID})
		}
		last = high
		return nil
	}
	// A failed scan keeps `last`, so the retry rereads everything it missed.
	scanBackoff := workqueue.NewTypedItemExponentialFailureRateLimiter[string](500*time.Millisecond, 30*time.Second)
	var retry <-chan time.Time
	rescan := func() {
		if err := scan(); err != nil {
			slog.Warn("scan activity Sandboxes; retrying", "error", err)
			retry = time.After(scanBackoff.When("scan"))
			return
		}
		scanBackoff.Forget("scan")
		retry = nil
	}
	rescan()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-retry:
			rescan()
		case <-changed:
			if !o.Hub.Connected() {
				continue
			}
			if current := o.Hub.Generation(); current != generation {
				generation = current
				last = 0
			}
			rescan()
		}
	}
}

func (o *HostActivityObserver) release(ctx context.Context, owner store.ActivityOwner) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := o.Store.ReleaseActivity(ctx, owner); err != nil && ctx.Err() == nil {
		slog.Warn("release activity lease", "error", err)
	}
}

func (o *HostActivityObserver) environmentState(ctx context.Context, key SandboxKey) (dispatcherpb.SessionActivity_EnvironmentDown_Cause, SandboxSnapshot, error) {
	snapshot := SandboxSnapshot{Name: key.Name, UID: key.UID, Namespace: o.Namespace}
	object, found, err := o.Catalog.GetEnvironment(ctx, snapshot)
	if err != nil {
		return 0, snapshot, err
	}
	if !found {
		return dispatcherpb.SessionActivity_EnvironmentDown_DELETED, snapshot, nil
	}
	if object.Status.OperatingMode == sandboxapi.OperatingModeSuspended && object.Status.StopStage == sandboxapi.StopStageStopped {
		return dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED, snapshot, nil
	}
	// Use the same Ready projection as delivery after the uncached lifecycle check.
	live, found, err := o.Catalog.GetSandbox(ctx, key.Name)
	if err != nil {
		return 0, snapshot, err
	}
	if !found || live.UID != key.UID {
		snapshot.Blocker = "sandbox_not_ready"
		return 0, snapshot, nil
	}
	return 0, live, nil
}

// The sampler uses the same lease, epoch and stream lifecycle as hosted Sessions,
// with one key for the service instead of one key per executor Sandbox.
func (o *HostActivityObserver) observeSampler(ctx context.Context) {
	backoff := workqueue.NewTypedItemExponentialFailureRateLimiter[string](500*time.Millisecond, 30*time.Second)
	for ctx.Err() == nil {
		owner, err := o.Store.AcquireSamplerActivity(ctx)
		if err == nil {
			owner, err = o.Store.BeginActivityEpoch(ctx, owner, true)
			if err == nil {
				// A stream ending after an epoch began (lease end, sampler roll) is not a failure.
				backoff.Forget("sampler")
				o.observe(ctx, owner, SandboxSnapshot{}, 0, o.Sampler.WatchActivity)
			}
			o.release(context.WithoutCancel(ctx), owner)
		}
		delay := backoff.When("sampler")
		var held *lease.HeldError
		if errors.As(err, &held) {
			delay = time.Until(held.Until)
			backoff.Forget("sampler")
		} else if err != nil && ctx.Err() == nil {
			slog.Warn("acquire sampler activity lease", "error", err)
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
	}
}

func (o *HostActivityObserver) observe(ctx context.Context, owner store.ActivityOwner, sandbox SandboxSnapshot, down dispatcherpb.SessionActivity_EnvironmentDown_Cause, watch func(context.Context, func(*harnesshostpb.WatchSessionsResponse) error) error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	renewed := make(chan struct{})
	go func() {
		defer close(renewed)
		timer := time.NewTicker(store.LeaseRenewInterval)
		defer timer.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-timer.C:
				if down == dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED {
					current, _, err := o.environmentState(ctx, SandboxKey{Name: sandbox.Name, UID: sandbox.UID})
					if err != nil || current != down {
						cancel()
						return
					}
				}
				err := o.Store.RenewActivity(ctx, owner)
				if err != nil {
					cancel()
					return
				}
			}
		}
	}()
	pending := map[string]*harnesshostpb.Session{}
	var err error
	if down == dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED {
		<-ctx.Done()
		err = ctx.Err()
	} else {
		err = watch(ctx, func(frame *harnesshostpb.WatchSessionsResponse) error {
			switch update := frame.Update.(type) {
			case *harnesshostpb.WatchSessionsResponse_Upsert:
				if update.Upsert.GetSessionId() == "" || !owner.Matches(update.Upsert) {
					return errors.New("invalid HarnessHost Session placement")
				}
				pending[update.Upsert.GetSessionId()] = update.Upsert
				return nil
			case *harnesshostpb.WatchSessionsResponse_CaughtUp:
				if err := o.Store.RecordHostActivity(ctx, owner, pending); err != nil {
					return err
				}
				clear(pending)
				return nil
			default:
				return fmt.Errorf("unknown HarnessHost watch frame %T", frame.Update)
			}
		})
	}
	cancel()
	<-renewed
	cleanup, end := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer end()
	if lost := o.Store.LoseActivityStream(cleanup, owner); lost != nil && !errors.Is(lost, lease.ErrLost) {
		slog.Warn("invalidate lost activity stream", "sandbox_uid", sandbox.UID, "error", lost)
	}
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Debug("HarnessHost watch ended", "sandbox_uid", sandbox.UID, "error", err)
	}
}
