package streaming

import (
	"context"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

type openGate struct{}

func (openGate) Recheck(context.Context) error { return nil }

func (openGate) DrainingContext() context.Context { return context.Background() }

type row struct {
	id       uuid.UUID
	version  int64
	selected bool
}

type frame struct {
	kind string
	rows []row
}

// table is the watched rows, and the reads the watch made of them.
type table struct {
	mu       sync.Mutex
	rows     map[uuid.UUID]row
	versions int
	found    [][]uuid.UUID
}

func (tb *table) put(r row) {
	tb.mu.Lock()
	defer tb.mu.Unlock()
	tb.rows[r.id] = r
}

func (tb *table) set() []row {
	var set []row
	for _, r := range tb.rows {
		if r.selected {
			set = append(set, r)
		}
	}
	slices.SortFunc(set, func(a, b row) int { return slices.Compare(a.id[:], b.id[:]) })
	return set
}

// watching runs a watch of tb's selected rows and returns its wakeups, its pending Changed answer and its frames.
func watching(t *testing.T, tb *table, keyed bool) (chan<- struct{}, func(ids []uuid.UUID, known bool), <-chan frame) {
	t.Helper()
	updates := make(chan struct{}, 1)
	frames := make(chan frame, 16)
	var mu sync.Mutex
	var pending []uuid.UUID
	known := true
	watch := VersionedSetWatch[row, frame]{
		Gate:    openGate{},
		Updates: updates,
		Send: func(f *frame) error {
			frames <- *f
			return nil
		},
		List: func(_ context.Context, after *row, limit int) ([]row, error) {
			tb.mu.Lock()
			defer tb.mu.Unlock()
			set := tb.set()
			if after != nil {
				set = slices.DeleteFunc(set, func(r row) bool { return slices.Compare(r.id[:], after.id[:]) <= 0 })
			}
			return set[:min(limit, len(set))], nil
		},
		Versions: func(context.Context) (map[uuid.UUID]int64, error) {
			tb.mu.Lock()
			defer tb.mu.Unlock()
			tb.versions++
			versions := map[uuid.UUID]int64{}
			for _, r := range tb.set() {
				versions[r.id] = r.version
			}
			return versions, nil
		},
		Find: func(_ context.Context, ids []uuid.UUID) ([]row, error) {
			tb.mu.Lock()
			defer tb.mu.Unlock()
			tb.found = append(tb.found, slices.Clone(ids))
			var found []row
			for _, id := range ids {
				if r, ok := tb.rows[id]; ok {
					found = append(found, r)
				}
			}
			return found, nil
		},
		Key:              func(r row) Versioned { return Versioned{ID: r.id, Version: r.version} },
		Selects:          func(r row) bool { return r.selected },
		Snapshot:         func(rows []row) (*frame, error) { return &frame{kind: "snapshot", rows: rows}, nil },
		SnapshotComplete: &frame{kind: "complete"},
		Upsert:           func(r row) (*frame, error) { return &frame{kind: "upsert", rows: []row{r}}, nil },
	}
	if keyed {
		watch.Changed = func() ([]uuid.UUID, bool) {
			mu.Lock()
			defer mu.Unlock()
			ids, k := pending, known
			pending, known = nil, true
			return ids, k
		}
	}
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- watch.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		<-done
	})
	return updates, func(ids []uuid.UUID, k bool) {
		mu.Lock()
		defer mu.Unlock()
		pending, known = ids, k
	}, frames
}

func next(t *testing.T, frames <-chan frame) frame {
	t.Helper()
	select {
	case f := <-frames:
		return f
	case <-time.After(10 * time.Second):
		t.Fatal("no frame")
		return frame{}
	}
}

func wantUpsert(t *testing.T, frames <-chan frame, want row) {
	t.Helper()
	if f := next(t, frames); f.kind != "upsert" || len(f.rows) != 1 || f.rows[0] != want {
		t.Fatalf("frame %+v, want an upsert of %+v", f, want)
	}
}

func TestAKeyedWatchReadsOnlyTheNamedRowsAndDropsOneThatLeft(t *testing.T) {
	stays, leaves, unnamed := uuid.New(), uuid.New(), uuid.New()
	tb := &table{rows: map[uuid.UUID]row{
		stays:   {id: stays, version: 1, selected: true},
		leaves:  {id: leaves, version: 1, selected: true},
		unnamed: {id: unnamed, version: 1, selected: true},
	}}
	updates, announce, frames := watching(t, tb, true)
	if f := next(t, frames); f.kind != "snapshot" || len(f.rows) != 3 {
		t.Fatalf("first frame %+v, want a snapshot of 3", f)
	}
	if f := next(t, frames); f.kind != "complete" {
		t.Fatalf("frame %+v, want snapshot_complete", f)
	}

	// A change no notification names is not read.
	tb.put(row{id: unnamed, version: 2, selected: true})
	tb.put(row{id: leaves, version: 2, selected: false})
	announce([]uuid.UUID{leaves}, true)
	updates <- struct{}{}
	wantUpsert(t, frames, row{id: leaves, version: 2, selected: false})
	// Once dropped, a row outside the set is not sent again.
	tb.put(row{id: leaves, version: 3, selected: false})
	tb.put(row{id: stays, version: 2, selected: true})
	announce([]uuid.UUID{leaves, stays}, true)
	updates <- struct{}{}
	wantUpsert(t, frames, row{id: stays, version: 2, selected: true})

	tb.mu.Lock()
	defer tb.mu.Unlock()
	if tb.versions != 0 {
		t.Errorf("the watch read every version %d times, want never", tb.versions)
	}
	if want := [][]uuid.UUID{{leaves}, {leaves, stays}}; !slices.EqualFunc(tb.found, want, slices.Equal) {
		t.Errorf("found %v, want %v", tb.found, want)
	}
}

func TestAWatchThatCannotNameTheChangesComparesEveryVersion(t *testing.T) {
	for _, keyed := range []bool{false, true} {
		changed, quiet := uuid.New(), uuid.New()
		tb := &table{rows: map[uuid.UUID]row{
			changed: {id: changed, version: 1, selected: true},
			quiet:   {id: quiet, version: 1, selected: true},
		}}
		updates, announce, frames := watching(t, tb, keyed)
		next(t, frames)
		next(t, frames)
		tb.put(row{id: changed, version: 2, selected: true})
		announce(nil, false)
		updates <- struct{}{}
		wantUpsert(t, frames, row{id: changed, version: 2, selected: true})
		// Had the unchanged row been sent too, it would arrive before this one.
		tb.put(row{id: quiet, version: 2, selected: true})
		announce(nil, false)
		updates <- struct{}{}
		wantUpsert(t, frames, row{id: quiet, version: 2, selected: true})
	}
}
