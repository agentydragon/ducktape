package streaming

import (
	"bytes"
	"cmp"
	"context"
	"errors"
	"slices"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/status"
)

const (
	// SnapshotChunk bounds the items in one snapshot frame of a watch.
	SnapshotChunk = 64
	// RecheckInterval is how often a watch with nothing to send checks its bearer again.
	RecheckInterval = 5 * time.Second
	// WriteTimeout bounds waiting for a watch Send to clear transport backpressure.
	WriteTimeout = 5 * time.Second
)

// Gate is what a watch keeps checking its call against: the caller's bearer, and the service's drain.
type Gate interface {
	// Recheck verifies the call's bearer again. It fails, with the status the call ends with, once the bearer is no
	// longer accepted.
	Recheck(ctx context.Context) error
	// DrainingContext is cancelled once the service stops accepting work.
	DrainingContext() context.Context
}

// WatchWriteContext is the context of a watch's ordinary writes: ctx, also cancelled once gate drains, so a write
// blocked on a client that does not read does not hold the drain up. Call stop when the watch ends.
func WatchWriteContext(ctx context.Context, gate Gate) (writeCtx context.Context, stop func()) {
	writeCtx, cancel := context.WithCancel(ctx)
	stopOnDrain := context.AfterFunc(gate.DrainingContext(), cancel)
	return writeCtx, func() {
		stopOnDrain()
		cancel()
	}
}

// SendWatchFrame checks the bearer of the call ctx again, then sends frame within WriteTimeout on writeCtx, with
// SendWithinStatus's statuses. A write that the drain cut short, while the call itself goes on, fails with draining.
func SendWatchFrame[Frame any](ctx, writeCtx context.Context, gate Gate, send func(*Frame) error, frame *Frame, timeoutMessage string, draining error) error {
	if err := gate.Recheck(ctx); err != nil {
		return err
	}
	err := SendWithin(writeCtx, WriteTimeout, send, frame)
	if errors.Is(err, context.Canceled) && ctx.Err() == nil && gate.DrainingContext().Err() != nil {
		return draining
	}
	return sendStatus(err, timeoutMessage)
}

// Versioned is what a VersionedSetWatch compares of an item: which it is, its version, and when it last changed.
type Versioned struct {
	ID        uuid.UUID
	Version   int64
	UpdatedAt time.Time
}

// VersionedSetWatch serves a watch of a set of versioned rows: snapshot frames of the set, snapshot_complete, then an
// upsert of each item whose version is not the one the client holds, in the order they changed. On a wakeup it reads
// only the items Changed names; without Changed, or when Changed cannot name them, it reads every item's version and
// then only the items whose version differs. The wakeup that follows a reconnection of the LISTEN connection is one
// too, so a change made while it was down is sent late, never lost.
// The bearer is checked again before every frame and every RecheckInterval while idle. An idle watch renews with
// ReconnectRequired when the service drains; a write the drain cuts short ends the watch with Draining.
type VersionedSetWatch[Item, Frame any] struct {
	Gate Gate
	// Updates wakes the watch after each commit that may change the set. It must be subscribed before Run, so a
	// commit between the subscription and the first read is not missed.
	Updates <-chan struct{}
	Send    func(*Frame) error
	// TimeoutMessage is the DEADLINE_EXCEEDED message of a write that outlasts WriteTimeout.
	TimeoutMessage string
	// Draining is what the watch ends with when the drain cuts a write short.
	Draining error

	// List returns at most limit items of the set in a stable order, starting after the item after, or from the
	// first when after is nil.
	List func(ctx context.Context, after *Item, limit int) ([]Item, error)
	// Versions returns the version of each item in the set.
	Versions func(ctx context.Context) (map[uuid.UUID]int64, error)
	// Changed names the items changed since its previous call, or since the watch subscribed; known is false when it
	// cannot, such as after a gap in the notifications. Nil when no wakeup names its items.
	Changed func() (ids []uuid.UUID, known bool)
	// Find returns the items among ids that the caller may still read, in any order, whether or not they are in the
	// set.
	Find func(ctx context.Context, ids []uuid.UUID) ([]Item, error)
	Key  func(Item) Versioned
	// Selects reports whether a found item is in the set. An upsert of one that is not tells the client to drop it.
	// Nil when every found item is.
	Selects func(Item) bool

	Snapshot          func([]Item) (*Frame, error)
	SnapshotComplete  *Frame
	Upsert            func(Item) (*Frame, error)
	ReconnectRequired *Frame
}

// Run serves the watch until it fails, the call ends, or the service drains.
func (w *VersionedSetWatch[Item, Frame]) Run(ctx context.Context) error {
	writeCtx, stopWrites := WatchWriteContext(ctx, w.Gate)
	defer stopWrites()
	r := setWatchRun[Item, Frame]{VersionedSetWatch: w, ctx: ctx, writeCtx: writeCtx, shown: map[uuid.UUID]int64{}}
	if err := r.snapshot(); err != nil {
		return err
	}
	recheck := time.NewTicker(RecheckInterval)
	defer recheck.Stop()
	for {
		select {
		case <-w.Updates:
			if err := r.upsertChanges(); err != nil {
				return err
			}
		case <-w.Gate.DrainingContext().Done():
			// A planned renewal: the client connects again, to a replica that is not going away.
			return SendWatchFrame(ctx, ctx, w.Gate, w.Send, w.ReconnectRequired, w.TimeoutMessage, w.Draining)
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-recheck.C:
			if err := w.Gate.Recheck(ctx); err != nil {
				return err
			}
		}
	}
}

type setWatchRun[Item, Frame any] struct {
	*VersionedSetWatch[Item, Frame]
	ctx      context.Context
	writeCtx context.Context
	// shown is the version the client holds of each item in the set.
	shown map[uuid.UUID]int64
}

func (r *setWatchRun[Item, Frame]) send(frame *Frame) error {
	return SendWatchFrame(r.ctx, r.writeCtx, r.Gate, r.Send, frame, r.TimeoutMessage, r.Draining)
}

func (r *setWatchRun[Item, Frame]) snapshot() error {
	var after *Item
	for {
		items, err := r.List(r.ctx, after, SnapshotChunk)
		if err != nil {
			return err
		}
		if len(items) > 0 {
			frame, err := r.Snapshot(items)
			if err != nil {
				return err
			}
			if err := r.send(frame); err != nil {
				return err
			}
			for _, item := range items {
				key := r.Key(item)
				r.shown[key.ID] = key.Version
			}
		}
		if len(items) < SnapshotChunk {
			break
		}
		after = &items[len(items)-1]
	}
	return r.send(r.SnapshotComplete)
}

// upsertChanges sends an upsert of each item whose version is not the one the client holds: one that entered the set
// or changed in it, and one that left it.
func (r *setWatchRun[Item, Frame]) upsertChanges() error {
	changed, err := r.changed()
	if err != nil || len(changed) == 0 {
		return err
	}
	items, err := r.Find(r.ctx, changed)
	if err != nil {
		return err
	}
	slices.SortFunc(items, func(a, b Item) int {
		ka, kb := r.Key(a), r.Key(b)
		return cmp.Or(ka.UpdatedAt.Compare(kb.UpdatedAt), bytes.Compare(ka.ID[:], kb.ID[:]))
	})
	for _, item := range items {
		key := r.Key(item)
		shown, held := r.shown[key.ID]
		selected := r.Selects == nil || r.Selects(item)
		if selected && held && shown == key.Version || !selected && !held {
			continue
		}
		frame, err := r.Upsert(item)
		if err != nil {
			return err
		}
		if err := r.send(frame); err != nil {
			return err
		}
		if selected {
			r.shown[key.ID] = key.Version
		} else {
			delete(r.shown, key.ID)
		}
	}
	return nil
}

// changed is the items that may differ from what the client holds: those Changed names, or else each item whose
// version is not the client's, including one that left the set.
func (r *setWatchRun[Item, Frame]) changed() ([]uuid.UUID, error) {
	if r.Changed != nil {
		if ids, known := r.Changed(); known {
			return ids, nil
		}
	}
	versions, err := r.Versions(r.ctx)
	if err != nil {
		return nil, err
	}
	var changed []uuid.UUID
	for id, version := range versions {
		if r.shown[id] != version {
			changed = append(changed, id)
		}
	}
	for id := range r.shown {
		if _, still := versions[id]; !still {
			changed = append(changed, id)
		}
	}
	return changed, nil
}
