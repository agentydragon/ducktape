package rules

import (
	"encoding/json"

	"github.com/invopop/jsonschema"
)

// openAPI describes the rules endpoint for the agents that read /openapi.json. Its schemas are
// reflected from the types that encode the responses, so the description cannot drift from them.
func openAPI() ([]byte, error) {
	// DoNotReference inlines every type, so a schema is usable wherever it is placed.
	reflector := jsonschema.Reflector{Anonymous: true, DoNotReference: true}
	failure := reflector.Reflect(errorBody{})
	rules := reflector.Reflect(EgressView{})
	// Only the root of a standalone schema document names its dialect.
	failure.Version, rules.Version = "", ""
	response := func(description string, schema *jsonschema.Schema) map[string]any {
		return map[string]any{
			"description": description,
			"content":     map[string]any{"application/json": map[string]any{"schema": schema}},
		}
	}
	return json.Marshal(map[string]any{
		"openapi": "3.1.0",
		"info":    map[string]any{"title": "agentplane-egress-rules", "version": "1"},
		"paths": map[string]any{
			"/v1/rules": map[string]any{"get": map[string]any{
				"summary":     "Effective egress rules",
				"description": "The rules of the ServiceAccount the bearer proves, redacted: no credential value, and no Secret it is read from.",
				"operationId": "readRules",
				"security":    []any{map[string]any{"workloadBearer": []any{}}},
				"responses": map[string]any{
					"200": response("The caller's effective egress policies.", rules),
					"401": response("The bearer is missing, malformed, or does not prove a workload this proxy serves.", failure),
					"503": response("The bearer could not be checked.", failure),
				},
			}},
		},
		"components": map[string]any{"securitySchemes": map[string]any{
			"workloadBearer": map[string]any{"type": "http", "scheme": "bearer"},
		}},
	})
}
