package main

import (
	"context"
	"net/http"

	"github.com/danielgtaylor/huma/v2"
)

type frontendConfigOutput struct {
	Body FrontendConfig
}

// RegisterCoreRoutes adds the configuration-backed application endpoints. All routes in this
// group use the same caller authentication as the Python API router.
func RegisterCoreRoutes(server *HTTPServer, config Config) error {
	if err := RegisterAppConfigRoutes(server, config); err != nil {
		return err
	}
	api := server.ProtectedAPI()
	huma.Register[struct{}, frontendConfigOutput](api, huma.Operation{
		OperationID: "getFrontendConfig", Method: http.MethodGet, Path: "/frontend-config", Tags: []string{"frontend config"},
		Summary: "Read browser-safe deployment settings",
	}, func(context.Context, *struct{}) (*frontendConfigOutput, error) {
		return &frontendConfigOutput{Body: config.FrontendConfig}, nil
	})
	return nil
}
