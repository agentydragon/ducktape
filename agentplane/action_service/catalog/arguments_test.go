package catalog

import (
	"errors"
	"strings"
	"testing"
	"time"
)

// tanaSearchNodes is the part of Tana's `search_nodes` input schema that withdrew the whole `tana` group when the
// Go executor replaced the Python one: draft-07, and an `edited.by` email `pattern` written with lookaheads, which
// Go's regexp (RE2) cannot compile. The pattern is verbatim from the server's tools/list (2026-11-01).
const tanaSearchNodes = `{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "properties": {
    "query": {
      "type": "object",
      "properties": {
        "edited": {
          "type": "object",
          "properties": {
            "by": {
              "type": "string",
              "format": "email",
              "pattern": "^(?!\\.)(?!.*\\.\\.)([A-Za-z0-9_'+\\-\\.]*)[A-Za-z0-9_+-]@([A-Za-z0-9][A-Za-z0-9\\-]*\\.)+[A-Za-z]{2,}$"
            }
          }
        }
      }
    }
  }
}`

func editedBy(email string) map[string]any {
	return map[string]any{"query": map[string]any{"edited": map[string]any{"by": email}}}
}

func TestLookaheadPatternCompilesAndValidates(t *testing.T) {
	action := ActionDefinition{InputSchema: []byte(tanaSearchNodes)}

	for _, email := range []string{"rai@example.com", "first.last+tag@sub.example.org"} {
		if err := action.CheckArguments(editedBy(email)); err != nil {
			t.Errorf("%q should satisfy the schema: %v", email, err)
		}
	}
	// What the lookaheads exist to refuse: a leading dot and a doubled dot in the local part.
	for _, email := range []string{".rai@example.com", "ra..i@example.com", "not-an-email", "rai@example"} {
		if err := action.CheckArguments(editedBy(email)); !errors.Is(err, ErrInvalidArguments) {
			t.Errorf("%q should be refused with ErrInvalidArguments, got %v", email, err)
		}
	}
}

func TestPatternsRE2WroteStillWork(t *testing.T) {
	// The ECMAScript-mode engine also takes `(?i)`, so a pattern that compiled under RE2 keeps its meaning.
	action := ActionDefinition{InputSchema: []byte(`{"type":"object","properties":{"name":{"type":"string","pattern":"(?i)^\\w+\\d$"}}}`)}
	if err := action.CheckArguments(map[string]any{"name": "ABC1"}); err != nil {
		t.Fatalf("a pattern with an inline flag must compile and match: %v", err)
	}
	if err := action.CheckArguments(map[string]any{"name": "ABC"}); !errors.Is(err, ErrInvalidArguments) {
		t.Fatalf("expected ErrInvalidArguments, got %v", err)
	}
}

func TestAnInvalidPatternStillFailsCompilation(t *testing.T) {
	action := ActionDefinition{InputSchema: []byte(`{"type":"object","properties":{"x":{"type":"string","pattern":"("}}}`)}

	err := action.CheckArguments(map[string]any{"x": "a"})

	if err == nil || errors.Is(err, ErrInvalidArguments) || !strings.Contains(err.Error(), "input schema") {
		t.Fatalf("an invalid regex must make the schema invalid, not the arguments: %v", err)
	}
}

func TestBacktrackingPatternIsABoundedMismatch(t *testing.T) {
	// (a+)+ explodes on the trailing "b". It must end quickly and
	// count as no match, so the arguments are refused rather than the service hanging.
	action := ActionDefinition{InputSchema: []byte(`{"type":"object","properties":{"x":{"type":"string","pattern":"^(?=a)(a+)+$"}}}`)}

	start := time.Now()
	err := action.CheckArguments(map[string]any{"x": strings.Repeat("a", 64) + "b"})

	if !errors.Is(err, ErrInvalidArguments) {
		t.Fatalf("expected ErrInvalidArguments, got %v", err)
	}
	if elapsed := time.Since(start); elapsed > 5*patternTimeout {
		t.Fatalf("match was not bounded: took %v", elapsed)
	}
}
