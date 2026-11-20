package catalog

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

// The email pattern comes from Tana search_nodes' published tool input schema, not user data.
const tanaSchema = `{"$schema":"http://json-schema.org/draft-07/schema#","type":"object","properties":{"query":{"type":"object","properties":{"edited":{"type":"object","properties":{"by":{"type":"string","pattern":"^(?!\\.)(?!.*\\.\\.)([A-Za-z0-9_'+\\-\\.]*)[A-Za-z0-9_+-]@([A-Za-z0-9][A-Za-z0-9\\-]*\\.)+[A-Za-z]{2,}$"}}}}}}}`

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

func TestTanaSearchPatternAndExistingGoPatterns(t *testing.T) {
	schema := compileTestSchema(t, tanaSchema)
	for _, tc := range []struct {
		email string
		valid bool
	}{
		{"r@example.com", true},
		{"first.last+tag@sub.example.org", true},
		{".first@example.com", false},
		{"first..last@example.com", false},
		{"first'@example.com", false},
		{"not-an-email", false},
	} {
		value := map[string]any{"query": map[string]any{"edited": map[string]any{"by": tc.email}}}
		if got := schema.Validate(value) == nil; got != tc.valid {
			t.Errorf("%q: valid=%v, want %v", tc.email, got, tc.valid)
		}
	}
	// A Go-style named group compiled before this change and must still compile and retain its meaning.
	existing := compileTestSchema(t, `{"type":"string","pattern":"^(?P<name>[a-z]+)$"}`)
	if err := existing.Validate("abc"); err != nil {
		t.Fatalf("existing RE2 syntax: %v", err)
	}
	if err := existing.Validate("ABC"); err == nil {
		t.Fatal("RE2 pattern unexpectedly matched")
	}
	bad, err := jsonschema.UnmarshalJSON(strings.NewReader(`{"pattern":"("}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := CompileSchema(bad); err == nil {
		t.Fatal("invalid regex compiled")
	}
}

func TestPatternTimeoutRejectsWholeValidation(t *testing.T) {
	const pattern = `^(?=a)(a+)+$`
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
			} else if err := schema.Validate("safe"); err != nil {
				t.Errorf("valid request incorrectly rejected: %v", err)
			}
		}(i)
	}
	wg.Wait()
}
