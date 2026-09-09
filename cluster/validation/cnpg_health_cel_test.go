package validation

import (
	"os"
	"testing"

	"github.com/bazelbuild/rules_go/go/runfiles"
	"github.com/google/cel-go/cel"
	"sigs.k8s.io/yaml"
)

func TestCNPGDatabaseHealthExpression(t *testing.T) {
	path, err := runfiles.Rlocation(os.Getenv("TEST_WORKSPACE") + "/cluster/k8s/tofu-state/db/flux-kustomization.yaml")
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		Spec struct {
			HealthCheckExprs []struct {
				APIVersion string `json:"apiVersion"`
				Kind       string `json:"kind"`
				Current    string `json:"current"`
			} `json:"healthCheckExprs"`
		} `json:"spec"`
	}
	if err := yaml.Unmarshal(data, &manifest); err != nil {
		t.Fatal(err)
	}
	var expression string
	for _, check := range manifest.Spec.HealthCheckExprs {
		if check.APIVersion == "postgresql.cnpg.io/v1" && check.Kind == "Database" {
			expression = check.Current
		}
	}
	if expression == "" {
		t.Fatal("missing CNPG Database current expression")
	}
	// Flux parses health expressions without predefined variables, then evaluates
	// against the resource's top-level fields (fluxcd/pkg runtime/cel).
	env, err := cel.NewEnv()
	if err != nil {
		t.Fatal(err)
	}
	ast, issues := env.Parse(expression)
	if issues.Err() != nil {
		t.Fatalf("parse deployed health expression: %v", issues.Err())
	}
	program, err := env.Program(ast)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name    string
		status  map[string]any
		want    bool
		wantErr bool
	}{
		{"ready", map[string]any{"applied": true, "observedGeneration": int64(2)}, true, false},
		{"not applied", map[string]any{"applied": false, "observedGeneration": int64(2)}, false, false},
		{"stale generation", map[string]any{"applied": true, "observedGeneration": int64(1)}, false, false},
		{"missing applied", map[string]any{"observedGeneration": int64(2)}, false, false},
		{"missing observed generation", map[string]any{"applied": true}, false, false},
		{"empty status", map[string]any{}, false, false},
		{"absent status", nil, false, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			object := map[string]any{"metadata": map[string]any{"generation": int64(2)}}
			if test.status != nil {
				object["status"] = test.status
			}
			result, _, err := program.Eval(object)
			if test.wantErr {
				if err == nil {
					t.Fatal("missing top-level status must not report a healthy object")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if result.Value() != test.want {
				t.Errorf("health = %v, want %v", result.Value(), test.want)
			}
		})
	}
}
