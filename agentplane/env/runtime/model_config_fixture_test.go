package runtime

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"

	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	"google.golang.org/grpc"
)

// fixtureModelConfig lets the runtime lifecycle fixtures control a lookup, including holding it
// open while exercising admission races. This private HTTP fixture does not model the production
// transport, which is exercised separately over gRPC.
type fixtureModelConfig struct{ endpoint string }

func (f fixtureModelConfig) GetModelConfig(ctx context.Context, request *modelconfigpb.GetModelConfigRequest, _ ...grpc.CallOption) (*modelconfigpb.GetModelConfigResponse, error) {
	u, err := url.Parse(f.endpoint + "/test/model-config")
	if err != nil {
		return nil, err
	}
	q := u.Query()
	q.Set("model", request.GetModel())
	u.RawQuery = q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	var model *struct {
		Budget int64 `json:"total_context_budget_tokens"`
	}
	if err := json.NewDecoder(res.Body).Decode(&model); err != nil {
		return nil, err
	}
	result := &modelconfigpb.GetModelConfigResponse{}
	if model != nil {
		result.TotalContextBudgetTokens = &model.Budget
	}
	return result, nil
}
