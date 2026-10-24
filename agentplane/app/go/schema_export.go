package main

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"io"

	"github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/go/historydata"
	"github.com/agentydragon/ducktape/agentplane/app/go/policyaccess"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/app/go/threadcommands"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
)

// openapiCompatibilityJSON is the captured public OpenAPI contract from the former Python app.
// Huma's document is checked for route and method coverage, then the captured contract preserves
// public operation IDs, validation schemas, and response types for generated clients. Update it
// alongside intentional API contract changes.
//
//go:embed openapi_compat.json
var openapiCompatibilityJSON []byte

// exportOpenAPI verifies that the Huma routes have a corresponding public contract entry and
// writes the captured FastAPI contract used by /openapi.json. The zero-value services are
// registration placeholders: Huma reads the operation declarations without invoking handlers.
func exportOpenAPI(writer io.Writer) error {
	server := NewHTTPServer(nil)
	config := Config{}
	RegisterCoreRoutes(server, config)
	RegisterSandboxRoutes(server, config, (*sandboxes.Access)(nil))
	RegisterPolicyRoutes(server, (*sandboxes.Access)(nil), (*policyaccess.Access)(nil))
	RegisterSessionRoutes(server, config, nil, nil, nil, true)
	RegisterRunnerRoutes(server, nil)
	RegisterThreadReadRoutes(server, (*threads.Store)(nil), (*historydata.Store)(nil), nil, false, nil, nil)
	if err := threadcommands.RegisterRoutes(server.ProtectedAPI(), threadcommands.Dependencies{
		Threads: (*threads.Store)(nil), History: (*historydata.Store)(nil), Readiness: (*bridge.RunnerBridge)(nil),
		Dispatcher: (*bridge.DispatcherClient)(nil),
	}); err != nil {
		return fmt.Errorf("register thread command routes: %w", err)
	}
	RegisterOperatorRoutes(server, nil, nil, nil, nil, "")
	RegisterStatusRoutes(server, nil)

	if err := server.ValidateOpenAPICompatibility(); err != nil {
		return err
	}
	_, err := writer.Write(openapiCompatibilityJSON)
	return err
}

// ValidateOpenAPICompatibility ensures each registered Huma route has a matching operation in
// the captured public contract. Call it after all routes are registered and before serving.
func (s *HTTPServer) ValidateOpenAPICompatibility() error {
	encoded, err := json.Marshal(s.api.OpenAPI())
	if err != nil {
		return fmt.Errorf("encode Huma OpenAPI document: %w", err)
	}
	var document map[string]any
	if err := json.Unmarshal(encoded, &document); err != nil {
		return fmt.Errorf("decode Huma OpenAPI document: %w", err)
	}
	return validateOpenAPICompatibility(document)
}

func validateOpenAPICompatibility(document map[string]any) error {
	var compatibility map[string]any
	if err := json.Unmarshal(openapiCompatibilityJSON, &compatibility); err != nil {
		return fmt.Errorf("decode OpenAPI compatibility fragments: %w", err)
	}
	humaPaths, ok := document["paths"].(map[string]any)
	if !ok {
		return fmt.Errorf("Huma OpenAPI document has no paths object")
	}
	compatibilityPaths, ok := compatibility["paths"].(map[string]any)
	if !ok {
		return fmt.Errorf("OpenAPI compatibility contract has no paths object")
	}
	for path, rawOperations := range humaPaths {
		legacyRaw, exists := compatibilityPaths[path]
		if !exists {
			return fmt.Errorf("Huma route %q is absent from the public OpenAPI contract", path)
		}
		humaOperations, ok := rawOperations.(map[string]any)
		if !ok {
			return fmt.Errorf("Huma route %q has an invalid path item", path)
		}
		legacyOperations, ok := legacyRaw.(map[string]any)
		if !ok {
			return fmt.Errorf("compatibility route %q has an invalid path item", path)
		}
		for method := range humaOperations {
			switch method {
			case "get", "post", "put", "patch", "delete", "head", "options", "trace":
			default:
				continue
			}
			if _, exists := legacyOperations[method]; !exists {
				return fmt.Errorf("Huma operation %s %s is absent from the public OpenAPI contract", method, path)
			}
		}
	}
	return nil
}
