package main

import "embed"

// agentDocs holds the agent guides served at /llms.txt and /docs/agents/, under `docs/agents/`.
//
//go:embed docs/agents
var agentDocs embed.FS

// openAPI is the OpenAPI document of the `ActionService` routes, generated from its proto and served at
// /openapi.json.
//
//go:embed action_service_openapi.swagger.json
var openAPI []byte
