package reader

import (
	"maps"
	"testing"
	"time"
)

func TestAvailability(t *testing.T) {
	now := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	staleAfter := 3 * time.Minute
	healthy := func() *Freshness {
		freshness := NewFreshness(staleAfter, PolicySets, PolicyBindings)
		for _, kind := range freshness.kinds {
			freshness.RecordCycle(kind, now)
		}
		freshness.MarkSynced()
		return freshness
	}

	for name, c := range map[string]struct {
		freshness func() *Freshness
		at        time.Time
		want      bool
	}{
		"synced and fresh":                       {healthy, now.Add(staleAfter), true},
		"a cycle older than the staleness bound": {healthy, now.Add(staleAfter + time.Second), false},
		"a clock that moved backwards":           {healthy, now.Add(-time.Second), false},
		"before the first sync": {
			func() *Freshness {
				freshness := healthy()
				freshness.synced = false
				return freshness
			}, now, false,
		},
		"a kind that never completed a cycle": {
			func() *Freshness {
				freshness := healthy()
				delete(freshness.cycles, PolicySets)
				return freshness
			}, now, false,
		},
		"one wedged kind among fresh ones": {
			func() *Freshness {
				freshness := healthy()
				freshness.RecordCycle(PolicyBindings, now.Add(-time.Hour))
				return freshness
			}, now, false,
		},
		"draining": {
			func() *Freshness {
				freshness := healthy()
				freshness.MarkDraining()
				return freshness
			}, now, false,
		},
	} {
		t.Run(name, func(t *testing.T) {
			freshness := c.freshness()
			if got := freshness.Available(c.at); got != c.want {
				t.Errorf("Available(%v) = %v, want %v", c.at, got, c.want)
			}
			if got := freshness.Status(c.at).Available; got != c.want {
				t.Errorf("Status(%v).Available = %v, want %v", c.at, got, c.want)
			}
		})
	}
}

func TestStatusReportsEachCycleAge(t *testing.T) {
	now := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	freshness := NewFreshness(3*time.Minute, PolicySets, PolicyBindings)
	freshness.RecordCycle(PolicySets, now.Add(-time.Minute))
	freshness.RecordCycle(PolicyBindings, now.Add(-2*time.Minute))
	got := freshness.Status(now)
	if got.StaleAfter != 3*time.Minute || got.Synced || got.Draining {
		t.Errorf("Status = %+v", got)
	}
	if want := map[Kind]time.Duration{PolicySets: time.Minute, PolicyBindings: 2 * time.Minute}; !maps.Equal(got.Ages, want) {
		t.Errorf("Ages = %v, want %v", got.Ages, want)
	}
}
