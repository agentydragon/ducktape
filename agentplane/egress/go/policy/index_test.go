package policy

import (
	"testing"
	"time"
)

func TestAvailability(t *testing.T) {
	now := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	staleAfter := 3 * time.Minute
	healthy := func() *Index {
		index := NewIndex(staleAfter)
		for _, kind := range watchedKinds {
			index.RecordCycle(kind, now)
		}
		index.MarkSynced()
		return index
	}

	for name, c := range map[string]struct {
		index func() *Index
		at    time.Time
		want  bool
	}{
		"synced and fresh":                       {healthy, now.Add(staleAfter), true},
		"a cycle older than the staleness bound": {healthy, now.Add(staleAfter + time.Second), false},
		"a clock that moved backwards":           {healthy, now.Add(-time.Second), false},
		"before the first sync": {
			func() *Index {
				index := healthy()
				index.synced = false
				return index
			}, now, false,
		},
		"a kind that never completed a cycle": {
			func() *Index {
				index := healthy()
				delete(index.cycles, Secrets)
				return index
			}, now, false,
		},
		"one wedged kind among fresh ones": {
			func() *Index {
				index := healthy()
				index.RecordCycle(Bindings, now.Add(-time.Hour))
				return index
			}, now, false,
		},
		"draining": {
			func() *Index {
				index := healthy()
				index.MarkDraining()
				return index
			}, now, false,
		},
	} {
		t.Run(name, func(t *testing.T) {
			if got := c.index().Available(c.at); got != c.want {
				t.Errorf("Available(%v) = %v, want %v", c.at, got, c.want)
			}
		})
	}
}
