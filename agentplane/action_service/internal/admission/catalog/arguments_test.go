package catalog

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

func compileTestSchema(t *testing.T, text string) *CompiledSchema {
	t.Helper()
	document, err := jsonschema.UnmarshalJSON(strings.NewReader(text))
	if err != nil {
		t.Fatal(err)
	}
	compiled, err := CompileSchema(document)
	if err != nil {
		t.Fatal(err)
	}
	return compiled
}

func TestPatternsUseECMAScriptUnicode(t *testing.T) {
	for _, tc := range []struct {
		pattern, matching, rejected string
	}{
		{`^\s$`, "\u00a0", "\u0085"},
		{`^.$`, "😀", "\u2028"},
		{`^(?=\p{Emoji}$)\u{1F600}$`, "😀", "😃"},
		{`^(?<first>a)(b)\1$`, "aba", "abb"},
	} {
		t.Run(tc.pattern, func(t *testing.T) {
			schema := compileTestSchema(t, fmt.Sprintf(`{"type":"string","pattern":%q}`, tc.pattern))
			if err := schema.Validate(tc.matching); err != nil {
				t.Errorf("%q does not match: %v", tc.matching, err)
			}
			if err := schema.Validate(tc.rejected); err == nil {
				t.Errorf("%q unexpectedly matches", tc.rejected)
			}
		})
	}
}

func TestPatternTimeoutRejectsWholeValidation(t *testing.T) {
	const pattern = `^(a+)+$`
	slow := strings.Repeat("a", 96) + "!"
	cases := []struct {
		name, schema string
		value        any
	}{
		{"not", fmt.Sprintf(`{"not":{"pattern":%q}}`, pattern), slow},
		{"anyOf", fmt.Sprintf(`{"anyOf":[{"pattern":%q},{"type":"string"}]}`, pattern), slow},
		{"patternProperties", fmt.Sprintf(`{"type":"object","patternProperties":{%q:{"type":"integer"}}}`, pattern), map[string]any{slow: "not-an-integer"}},
		{"if", fmt.Sprintf(`{"if":{"pattern":%q},"then":false,"else":true}`, pattern), slow},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			compiled := compileTestSchema(t, tc.schema)
			if err := compiled.Validate(tc.value); !errors.Is(err, ErrPatternEvaluation) {
				t.Fatalf("got %v, want ErrPatternEvaluation", err)
			}
		})
	}
}

func TestConcurrentValidationsKeepPatternErrorsSeparate(t *testing.T) {
	schema := compileTestSchema(t, `{"not":{"pattern":"^(?=a)(a+)+$"}}`)
	slow := strings.Repeat("a", 96) + "!"
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if i%4 == 0 {
				if err := schema.Validate(slow); !errors.Is(err, ErrPatternEvaluation) {
					t.Errorf("timeout got %v", err)
				}
			} else if err := schema.Validate("safe"); err != nil && !errors.Is(err, ErrPatternEvaluation) {
				t.Errorf("valid request returned unexpected error: %v", err)
			}
		}(i)
	}
	wg.Wait()
	if err := schema.Validate("safe"); err != nil {
		t.Errorf("validation remained poisoned after concurrent requests: %v", err)
	}
}

// The regex format compiles its value at validation time, but never matches against it. It must not introduce
// shared match state into a schema without patterns.
func TestRegexFormatCanCompileConcurrently(t *testing.T) {
	schema := compileTestSchema(t, `{"$schema":"http://json-schema.org/draft-07/schema#","type":"string","format":"regex"}`)
	if schema.gate != nil {
		t.Fatal("format-only schema unexpectedly requires matcher gate")
	}
	var wg sync.WaitGroup
	for range 20 {
		wg.Go(func() {
			if err := schema.Validate("(?=a)"); err != nil {
				t.Errorf("valid regex format rejected: %v", err)
			}
		})
	}
	wg.Wait()
}

func TestSaturatedPatternGateFailsClosed(t *testing.T) {
	schema := compileTestSchema(t, `{"not":{"pattern":"^(?=a)(a+)+$"}}`)
	if schema.gate == nil {
		t.Fatal("pattern schema has no gate")
	}
	// Hold the slot deterministically so the request exercises waiting, not a slow match.
	schema.gate <- struct{}{}
	if err := schema.Validate("safe"); !errors.Is(err, ErrPatternEvaluation) {
		t.Fatalf("saturated gate returned %v; want ErrPatternEvaluation", err)
	}
	<-schema.gate
	if err := schema.Validate("safe"); err != nil {
		t.Fatalf("validation remained poisoned after saturation: %v", err)
	}
}

func TestPatternFailureSurvivesArgumentChecking(t *testing.T) {
	const schema = `{"type":"object","properties":{"value":{"not":{"pattern":"^(?=a)(a+)+$"}}}}`
	definition := ActionDefinition{InputSchema: json.RawMessage(schema)}
	value := map[string]any{"value": strings.Repeat("a", 96) + "!"}
	if err := definition.CheckArguments(value); !errors.Is(err, ErrPatternEvaluation) {
		t.Fatalf("CheckArguments returned %v; want pattern failure", err)
	}
	compiled := compileTestSchema(t, schema)
	wire, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := DecodeArguments(compiled, wire, &decoded); !errors.Is(err, ErrPatternEvaluation) {
		t.Fatalf("DecodeArguments returned %v; want pattern failure", err)
	}
}
