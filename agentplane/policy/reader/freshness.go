// Package reader reads PolicySets for a Go service that enforces one rule type: an Index of that
// type's rules and the bindings that grant them, kept equal to the API server by shared informers
// whose Freshness says when it may be acted on, and the service's condition on each PolicyBinding.
package reader

import (
	"sync"
	"time"
)

// Kind is a watched resource kind, by plural.
type Kind string

const (
	PolicySets     Kind = "policysets"
	PolicyBindings Kind = "policybindings"
)

// Freshness says whether a reader's view of the API server may be acted on.
type Freshness struct {
	staleAfter time.Duration
	kinds      []Kind

	mu sync.RWMutex
	// cycles holds when each watched kind last completed a full list-and-watch cycle. Under health
	// every entry advances at the watch period; one that stops is the failure this exists to
	// expose: a wedged list or a watch that never returns leaves the view frozen while every
	// answer it gives stays plausible and `synced` stays true.
	cycles   map[Kind]time.Time
	synced   bool
	draining bool
}

// NewFreshness returns a Freshness over the given kinds that stops being available once a kind's
// last completed cycle is older than staleAfter.
func NewFreshness(staleAfter time.Duration, kinds ...Kind) *Freshness {
	return &Freshness{staleAfter: staleAfter, kinds: kinds, cycles: map[Kind]time.Time{}}
}

// Available reports whether the view may be acted on: it has finished its first sync, every
// watched kind completed a cycle recently, and the reader is not draining.
func (f *Freshness) Available(now time.Time) bool {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.available(now)
}

// Status is the facts Available rests on, read at one moment.
type Status struct {
	Available bool
	// Synced is whether every watched kind has been listed once.
	Synced   bool
	Draining bool
	// StaleAfter is how long a watched kind may go without completing a cycle.
	StaleAfter time.Duration
	// Ages is the time since each recorded kind last completed a cycle.
	Ages map[Kind]time.Duration
}

// Status reads the verdict and the ages it rests on under one lock, so they cannot disagree.
func (f *Freshness) Status(now time.Time) Status {
	f.mu.RLock()
	defer f.mu.RUnlock()
	ages := make(map[Kind]time.Duration, len(f.cycles))
	for kind, completed := range f.cycles {
		ages[kind] = now.Sub(completed)
	}
	return Status{
		Available: f.available(now), Synced: f.synced, Draining: f.draining, StaleAfter: f.staleAfter, Ages: ages,
	}
}

func (f *Freshness) available(now time.Time) bool {
	if f.draining || !f.synced {
		return false
	}
	for _, kind := range f.kinds {
		// A negative age is a clock that moved backwards, which is no more trustworthy than a late one.
		completed, ok := f.cycles[kind]
		if age := now.Sub(completed); !ok || age < 0 || age > f.staleAfter {
			return false
		}
	}
	return true
}

// MarkSynced records that the first list of every kind has been applied. It never falls back;
// recent cycles are what keep the view available after.
func (f *Freshness) MarkSynced() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.synced = true
}

// RecordCycle records that kind completed a list-and-watch cycle at the given instant. Watches seeds
// every kind with its start time before listing, so a kind that never finishes one goes stale like
// any other.
func (f *Freshness) RecordCycle(kind Kind, at time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cycles[kind] = at
}

// MarkDraining makes the view unavailable for good: the reader admits nothing new.
func (f *Freshness) MarkDraining() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.draining = true
}
