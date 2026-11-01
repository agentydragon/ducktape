package main

import (
	"context"
	"net/http"
	"sort"

	appconfigpb "github.com/agentydragon/ducktape/agentplane/app/configservicepb"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/protobuf/types/known/emptypb"
)

// RegisterAppConfigRoutes serves the app-owned browser configuration through generated
// ProtoJSON HTTP routes. The generated gateway and service implementation share this process;
// there is no separate config service or internal HTTP hop.
func RegisterAppConfigRoutes(server *HTTPServer, config Config) error {
	gateway := runtime.NewServeMux()
	if err := appconfigpb.RegisterAppConfigServiceHandlerServer(context.Background(), gateway, &appConfigService{config: config}); err != nil {
		return err
	}
	const prefix = "/upstream/app-config"
	server.RegisterRawRoute(prefix+"/{path...}", http.StripPrefix(prefix, gateway), false)
	return nil
}

type appConfigService struct {
	appconfigpb.UnimplementedAppConfigServiceServer
	config Config
}

func (s *appConfigService) ListModels(context.Context, *emptypb.Empty) (*appconfigpb.ListModelsResponse, error) {
	response := &appconfigpb.ListModelsResponse{}
	for _, model := range s.config.Models.Models {
		response.Models = append(response.Models, &appconfigpb.ModelOption{
			Model: model.Model, DisplayName: model.DisplayName,
			ReasoningEfforts: nonNilStrings(model.ReasoningEfforts),
		})
	}

	harnessNames := make([]string, 0, len(s.config.Models.Harnesses))
	for name := range s.config.Models.Harnesses {
		harnessNames = append(harnessNames, name)
	}
	sort.Strings(harnessNames)
	for _, name := range harnessNames {
		value, ok := runnerpb.Harness_value[name]
		if !ok {
			// Config validation rejects unknown names before the server starts.
			continue
		}
		response.Harnesses = append(response.Harnesses, &appconfigpb.HarnessModelOffers{
			Harness: runnerpb.Harness(value), Models: nonNilStrings(s.config.Models.Harnesses[name]),
		})
	}
	return response, nil
}

func (s *appConfigService) ListSandboxPresets(context.Context, *emptypb.Empty) (*appconfigpb.ListSandboxPresetsResponse, error) {
	response := &appconfigpb.ListSandboxPresetsResponse{}
	for _, preset := range s.config.PresetViews() {
		defaults := &appconfigpb.SessionDefaults{
			Model: preset.SessionDefaults.Model, Cwd: preset.SessionDefaults.CWD,
			Instructions: preset.SessionDefaults.Instructions, SetupScript: preset.SessionDefaults.SetupScript,
		}
		if value, ok := runnerpb.Harness_value[preset.SessionDefaults.Harness]; ok {
			defaults.Harness = runnerpb.Harness(value)
		}
		if preset.SessionDefaults.ReasoningEffort != nil {
			defaults.ReasoningEffort = preset.SessionDefaults.ReasoningEffort
		}
		response.Presets = append(response.Presets, &appconfigpb.SandboxPreset{
			Name: preset.Name, Title: preset.Title, Template: preset.Template,
			PolicySets: nonNilStrings(preset.PolicySets), InboxTags: nonNilStrings(preset.InboxTags),
			SessionDefaults: defaults, Bootstrap: preset.Bootstrap,
		})
	}
	return response, nil
}
