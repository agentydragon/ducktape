package main

import _ "embed"

// openAPI is the OpenAPI document of the ActionService routes, generated from its proto for the
// temporary HTTP compatibility surface.
//
//go:embed action_service_openapi.swagger.json
var openAPI []byte
