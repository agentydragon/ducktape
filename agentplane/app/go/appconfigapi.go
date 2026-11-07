package main

import (
	"context"
	"net/http"
	"sort"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

type policySetNameReader interface {
	ListPolicySetNames(context.Context, *policypb.ListPolicySetNamesRequest, ...grpc.CallOption) (*policypb.ListPolicySetNamesResponse, error)
}

// RegisterAppConfigRoutes serves the app-owned browser configuration through generated
// ProtoJSON HTTP routes. The generated gateway and service implementation share this process;
// there is no separate config service or internal HTTP hop.
func RegisterAppConfigRoutes(server *HTTPServer, service *appService) error {
	gateway := runtime.NewServeMux()
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), gateway, service); err != nil {
		return err
	}
	const prefix = "/upstream/app-config"
	server.RegisterRawRoute("GET "+prefix+"/v1/models", http.StripPrefix(prefix, gateway), false)
	server.RegisterRawRoute("GET "+prefix+"/v1/presets", http.StripPrefix(prefix, gateway), false)
	server.RegisterRawRoute("GET "+prefix+"/v1/frontend-config", http.StripPrefix(prefix, gateway), false)
	server.RegisterRawRoute("GET "+prefix+"/v1/sandbox-form", http.StripPrefix(prefix, gateway), false)
	return nil
}

func (s *appService) GetSandboxForm(ctx context.Context, _ *apppb.GetSandboxFormRequest) (*apppb.GetSandboxFormResponse, error) {
	if s.policySetNames == nil {
		return nil, status.Error(codes.Unavailable, "Policy access is unavailable")
	}
	sets, err := s.policySetNames.ListPolicySetNames(ctx, &policypb.ListPolicySetNamesRequest{})
	if err != nil {
		return nil, status.Error(codes.Unavailable, "Policy access is unavailable")
	}
	defaults := &apppb.NewSandboxDefaults{WakeOnSend: s.config.NewSandboxDefaults.WakeOnSend}
	if seconds := s.config.NewSandboxDefaults.IdleSuspendAfterSeconds; seconds != nil {
		defaults.IdleSuspendAfterSeconds = seconds
	}
	return &apppb.GetSandboxFormResponse{
		PolicySets: nonNilStrings(sets.GetNames()), NewSandboxDefaults: defaults,
	}, nil
}

func (s *appService) ListModels(context.Context, *emptypb.Empty) (*apppb.ListModelsResponse, error) {
	response := &apppb.ListModelsResponse{}
	for _, model := range s.config.Models.Models {
		response.Models = append(response.Models, &apppb.ModelOption{
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
		value, ok := sessionpb.Harness_value[name]
		if !ok {
			// Config validation rejects unknown names before the server starts.
			continue
		}
		response.Harnesses = append(response.Harnesses, &apppb.HarnessModelOffers{
			Harness: sessionpb.Harness(value), Models: nonNilStrings(s.config.Models.Harnesses[name]),
		})
	}
	return response, nil
}

func (s *appService) ListSandboxPresets(context.Context, *emptypb.Empty) (*apppb.ListSandboxPresetsResponse, error) {
	response := &apppb.ListSandboxPresetsResponse{}
	for _, preset := range s.config.PresetViews() {
		defaults := &apppb.SessionDefaults{
			Model: preset.SessionDefaults.Model, Cwd: preset.SessionDefaults.CWD,
			Instructions: preset.SessionDefaults.Instructions, SetupScript: preset.SessionDefaults.SetupScript,
		}
		if value, ok := sessionpb.Harness_value[preset.SessionDefaults.Harness]; ok {
			defaults.Harness = sessionpb.Harness(value)
		}
		if preset.SessionDefaults.ReasoningEffort != nil {
			defaults.ReasoningEffort = preset.SessionDefaults.ReasoningEffort
		}
		response.Presets = append(response.Presets, &apppb.SandboxPreset{
			Name: preset.Name, Title: preset.Title, Template: preset.Template,
			PolicySets: nonNilStrings(preset.PolicySets), InboxTags: nonNilStrings(preset.InboxTags),
			SessionDefaults: defaults, Bootstrap: preset.Bootstrap,
		})
	}
	return response, nil
}

func (s *appService) GetFrontendConfig(context.Context, *apppb.GetFrontendConfigRequest) (*apppb.GetFrontendConfigResponse, error) {
	config := &apppb.FrontendConfig{}
	if settings := s.config.FrontendConfig.TraceSearch; settings != nil {
		config.TraceSearch = &apppb.TraceSearchSettings{
			GrafanaBaseUrl: settings.GrafanaBaseURL, TempoDatasourceUid: settings.TempoDatasourceUID,
			DeploymentEnvironment: settings.DeploymentEnvironment,
		}
	}
	if settings := s.config.FrontendConfig.LangfuseSearch; settings != nil {
		config.LangfuseSearch = &apppb.LangfuseSearchSettings{
			BaseUrl: settings.BaseURL, ProjectId: settings.ProjectID,
			SessionMetadataKey:               settings.SessionMetadataKey,
			SandboxNamespaceMetadataKey:      settings.SandboxNamespaceMetadataKey,
			SandboxServiceAccountMetadataKey: settings.SandboxServiceAccountMetadataKey,
		}
	}
	return &apppb.GetFrontendConfigResponse{FrontendConfig: config}, nil
}
