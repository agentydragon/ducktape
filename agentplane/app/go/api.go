package main

import (
	"context"
	"net/http"

	"github.com/danielgtaylor/huma/v2"
)

type listModelsOutput struct {
	Body ModelCatalog
}

type listPresetsOutput struct {
	Body []SandboxPresetView `nullable:"false"`
}

// RegisterCoreRoutes adds the configuration-backed application endpoints. All routes in this
// group use the same caller authentication as the Python API router.
func RegisterCoreRoutes(server *HTTPServer, config Config) {
	api := server.ProtectedAPI()
	huma.Register[struct{}, listModelsOutput](api, huma.Operation{
		OperationID: "listModels", Method: http.MethodGet, Path: "/models", Tags: []string{"models"},
		Summary: "List configured model and harness offers",
	}, func(context.Context, *struct{}) (*listModelsOutput, error) {
		return &listModelsOutput{Body: config.Models}, nil
	})
	huma.Register[struct{}, listPresetsOutput](api, huma.Operation{
		OperationID: "listPresets", Method: http.MethodGet, Path: "/presets", Tags: []string{"presets"},
		Summary: "List configured Sandbox presets and their thread defaults",
	}, func(context.Context, *struct{}) (*listPresetsOutput, error) {
		return &listPresetsOutput{Body: config.PresetViews()}, nil
	})
}
