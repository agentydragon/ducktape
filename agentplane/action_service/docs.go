package main

import "embed"

// openAPI is the OpenAPI document of the `ActionService` routes, generated from its proto and served at
// /openapi.json.
//
//go:embed action_service_openapi.swagger.json
var openAPI []byte
