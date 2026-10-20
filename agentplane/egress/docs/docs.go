// Package docs holds the agent-facing documentation the egress proxy serves.
package docs

import "embed"

// Agents holds the documentation served at /llms.txt and /docs/agents/, under `agents/`.
//
//go:embed agents
var Agents embed.FS

// OpenAPI is the OpenAPI document of the rules API, generated from its proto and served at /openapi.json.
//
//go:embed openapi.json
var OpenAPI []byte
