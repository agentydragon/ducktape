// Reconstructed from binary: environment-manager (Build ID 495ea204)
// Source: internal/claude/outcomes.go
// Original path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/claude/outcomes.go

package claude

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
)

// Outcomes is a thread-safe collection of outcome key-value pairs grouped by
// repository name. The inner map key is the repository name (string), and the
// value is a slice of OutcomeEntry (key-value string pairs).
//
// The struct holds a pointer to a map; a nil pointer means no outcomes recorded
// yet. The map is lazily initialized on first Add call.
type Outcomes struct {
	// data is *map[string][]OutcomeEntry — pointer to map so nil signals "empty"
	data *map[string][]OutcomeEntry
	mu   sync.Mutex
}

// OutcomeEntry represents a single outcome key-value pair within a repository.
type OutcomeEntry struct {
	Key   string
	Value string
}

// NewOutcomes creates a new empty Outcomes instance.
// NOTE: NewOutcomes does NOT appear in the nm symbol table, meaning it may
// be inlined or constructed directly at call sites. The Outcomes struct is
// allocated via runtime.newobject in callers.
func NewOutcomes() *Outcomes {
	return &Outcomes{}
}

// Add adds an outcome entry (key, value) for the given repository.
// If the internal map is nil, it is lazily initialized via makemap_small.
//
// Binary address: 0xae2f40 - 0xae30d9
func (o *Outcomes) Add(repo string, branch string) {
	// 0xae2f6b: CMPQ 0(AX), $0x0 — check if o.data == nil
	if o.data == nil {
		// 0xae2f71: runtime.makemap_small
		m := make(map[string][]OutcomeEntry)
		o.data = &m
	}

	// 0xae2fb4: TESTQ SI, SI — check if branch == ""
	// If branch is empty, uses a different OutcomeEntry representation
	entry := OutcomeEntry{
		Key:   repo,
		Value: branch,
	}

	// 0xae2ff8-0xae3050: mapassign_faststr — append entry to the slice for repo
	entries := (*o.data)[repo]
	entries = append(entries, entry)
	(*o.data)[repo] = entries
}

// Get returns the slice of OutcomeEntry values for a given repository.
// Returns nil if the outcomes map is nil or the repository is not present.
//
// Binary address: 0xae30e0 - 0xae3154
func (o *Outcomes) Get(repo string) []OutcomeEntry {
	if o.data == nil {
		return nil
	}
	// 0xae310b: mapaccess1_faststr
	return (*o.data)[repo]
}

// GetFirst returns the first OutcomeEntry for a given repository, or an empty
// OutcomeEntry if none exist.
//
// Binary address: 0xae3160 - 0xae31fc
func (o *Outcomes) GetFirst(repo string) OutcomeEntry {
	entries := o.Get(repo)
	if len(entries) > 0 {
		return entries[0]
	}
	return OutcomeEntry{}
}

// Repositories returns a slice of all repository names that have outcomes.
//
// Binary address: 0xae3200 - 0xae339c
func (o *Outcomes) Repositories() []string {
	if o.data == nil {
		return nil
	}
	// 0xae3238-0xae3390: iterate map keys, collect into slice
	var repos []string
	for repo := range *o.data {
		repos = append(repos, repo)
	}
	return repos
}

// AsMap returns a shallow copy of the internal map as map[string][]OutcomeEntry.
// Returns an empty map if the outcomes are nil or empty.
//
// Binary address: 0xae33a0 - 0xae355c
func (o *Outcomes) AsMap() map[string][]OutcomeEntry {
	result := make(map[string][]OutcomeEntry)
	if o == nil || o.data == nil {
		return result
	}
	for repo, entries := range *o.data {
		result[repo] = entries
	}
	return result
}

// Len returns the number of repositories with outcomes.
// Returns 0 if the internal map is nil.
//
// Binary address: 0xae3560 - 0xae3578
func (o *Outcomes) Len() int {
	if o.data == nil {
		return 0
	}
	return len(*o.data)
}

// IsEmpty returns true if there are no outcomes recorded.
//
// Binary address: 0xae3580 - 0xae359c
func (o *Outcomes) IsEmpty() bool {
	return o.Len() == 0
}

// Validate checks that all outcomes are valid. It calls ValidateWithLogger
// with a nil logger.
//
// Binary address: 0xae35a0 - 0xae35dc
func (o *Outcomes) Validate() *Outcomes {
	return o.ValidateWithLogger(nil)
}

// ValidateWithLogger iterates over all outcomes and validates each entry.
// If a repository has more than one outcome entry, it logs a warning.
// Returns a new Outcomes containing only the first entry for each repository.
//
// Binary address: 0xae35e0 - 0xae3ad3
func (o *Outcomes) ValidateWithLogger(logger *slog.Logger) *Outcomes {
	// 0xae3600: TESTQ AX, AX — check if o is nil
	// 0xae3609: CMPQ 0(AX), $0x0 — check if o.data is nil
	if o == nil || o.data == nil {
		// Return empty Outcomes
		result := make(map[string][]OutcomeEntry)
		validated := &Outcomes{data: &result}
		return validated
	}

	// 0xae3624: makemap_small — create result map
	result := make(map[string][]OutcomeEntry)
	validated := &Outcomes{data: &result}

	// 0xae36a2-0xae3705: mapIterStart + loop
	for repo, entries := range *o.data {
		if len(entries) == 0 {
			// 0xae399e-0xae3a14: log error level (-4 = ERROR)
			// Log message length 0x3b = 59 chars
			if logger != nil {
				logger.Log(context.Background(), slog.LevelError,
					"repository has no outcome entries, skipping validation",
					"repository", repo,
				)
			}
			continue
		}

		if len(entries) > 1 {
			// 0xae3740-0xae3886: log warn (level 0x3a = 58 decimal, but slog uses
			// different level mapping — this is slog.LevelWarn)
			// Log message with 6 attrs: repo, first entry key/value, remaining entries
			if logger != nil {
				logger.Log(context.Background(), slog.LevelWarn,
					"repository has multiple outcome entries, using first",
					"repository", repo,
					"firstEntry", entries[0],
					"remainingEntries", entries[1:],
				)
			}
		}

		// 0xae389d-0xae3996: take first entry, assign to result map
		first := entries[0]
		(*validated.data)[repo] = []OutcomeEntry{first}
	}

	return validated
}

// String returns a human-readable string representation of the Outcomes.
// Returns "Outcomes{empty}" if empty, otherwise "Outcomes{count: N}" where
// N is the number of repositories.
//
// Binary address: 0xae3b00 - 0xae3ba0
func (o *Outcomes) String() string {
	// 0xae3b12-0xae3b21: dereference o.data, check map length
	if o.data == nil || len(*o.data) == 0 {
		// 0xae3b7a: returns "Outcomes{empty}" (15 chars = 0xf)
		return "Outcomes{empty}"
	}
	count := len(*o.data)
	// 0xae3b56: format string length 0x1a = 26 chars
	return fmt.Sprintf("Outcomes{repositories: %d}", count)
}
