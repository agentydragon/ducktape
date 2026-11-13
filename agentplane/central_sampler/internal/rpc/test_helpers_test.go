package rpc

import (
	"context"

	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type fixtureReviews struct {
	uid, account string
	failure      error
}

func (r fixtureReviews) Create(ctx context.Context, request *authenticationv1.TokenReview, options metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	if r.failure != nil {
		return nil, r.failure
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{Authenticated: request.Spec.Token == "test-token", Audiences: request.Spec.Audiences, User: authenticationv1.UserInfo{Username: r.account, UID: r.uid, Extra: map[string]authenticationv1.ExtraValue{"authentication.kubernetes.io/pod-name": {"test-pod"}, "authentication.kubernetes.io/pod-uid": {"test-pod-uid"}}}}}, nil
}

func fixtureSettings() samplersettings.Settings {
	platformInstructions := "Fixture Agentplane guidance."
	return samplersettings.Settings{
		Models:      map[string]samplersettings.ModelConfig{"test-model": {Connection: "provider", ProviderModel: "test-model", API: protocol.APIStyleResponses, Responses: responses.Config{Dialect: responses.DialectLite}, ExecEncoding: protocol.ExecEncodingFreeform, ReasoningEfforts: []string{"", "high"}}},
		Connections: map[string]samplersettings.ConnectionConfig{"provider": {BaseURL: "https://model.invalid", Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "test-credential"}}},
		DatabaseURL: "postgresql://fixture.invalid/sampler", Listen: "127.0.0.1:0", Audience: "test-sampler", AllowedAccounts: []string{"test/client"}, PlatformInstructions: &platformInstructions, CodeModeHost: "/fixture/host", ExecutorCodex: "/fixture/codex", Logs: "/fixture/logs", DirectExecutorTarget: "127.0.0.1:4501", DirectExecutorInsecureLocaltest: true, MaxLiveSessions: 2, RPCTimeoutSeconds: 10, LeaseSeconds: 60,
	}
}
