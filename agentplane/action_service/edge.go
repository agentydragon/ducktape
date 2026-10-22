package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/authserver"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connections"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/mcpserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	mcpauth "github.com/modelcontextprotocol/go-sdk/auth" // workload auth and MCP auth are different authorities.
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// edgeCaller resolves the HTTP bearer on every core call and watch recheck. It never takes a
// ServiceAccount from tool arguments: only the authorization server's current Connection binding.
type edgeCaller struct{ authority *authserver.Server }

type edgeBearerKey struct{}

func (c edgeCaller) ResolveCaller(ctx context.Context) (auth.Account, error) {
	token, _ := ctx.Value(edgeBearerKey{}).(string)
	if token == "" {
		return auth.Account{}, status.Error(codes.Unauthenticated, "invalid bearer")
	}
	current, err := c.authority.VerifyBearer(ctx, token, nil)
	switch {
	case errors.Is(err, mcpauth.ErrInvalidToken):
		return auth.Account{}, status.Error(codes.Unauthenticated, "invalid bearer")
	case err != nil:
		return auth.Account{}, err
	}
	namespace, name, ok := strings.Cut(current.UserID, "/")
	if !ok || namespace == "" || name == "" {
		return auth.Account{}, errors.New("authorization server returned an invalid account")
	}
	return auth.Account{Namespace: namespace, Name: name}, nil
}

func mountMCPEdge(ctx context.Context, routes *http.ServeMux, c config, db *sql.DB,
	connectionStore *connections.Store, core *api.Service, gate *api.Gate,
) error {
	o := c.OAuth
	secret, err := os.ReadFile(o.UpstreamClientSecretFile)
	if err != nil {
		return errors.New("reading oauth.upstream_client_secret_file failed")
	}
	if strings.TrimSpace(string(secret)) == "" {
		return errors.New("oauth upstream client secret is empty")
	}
	authority, err := authserver.New(ctx, authserver.Config{
		Issuer: o.BaseURL, ConsentURL: o.IntegrationAppURL,
		UpstreamIssuer: o.UpstreamIssuer, UpstreamClientID: o.UpstreamClientID,
		UpstreamClientSecret: strings.TrimSpace(string(secret)), UpstreamSubject: o.UpstreamSubject,
		Approver:       connections.Operator{Issuer: o.ApprovingOperator.Issuer, Subject: o.ApprovingOperator.Subject},
		DocumentClient: authserver.NewDocumentClient(),
	}, db, connectionStore)
	if err != nil {
		return fmt.Errorf("starting external MCP authorization: %w", err)
	}
	client := api.NewLocalClient(core, gate, edgeCaller{authority: authority})
	mcp := mcpserver.NewHandler(client, authority.VerifyBearer, mcpserver.Options{
		MaxWaitSeconds: c.MaxWaitSeconds, DirectWaitSeconds: c.DirectWaitSeconds,
		ResourceMetadataURL: authority.ResourceMetadataURL(), Logger: slog.Default(),
	})
	routes.Handle("/mcp", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token, _ := auth.BearerToken(r.Header.Values("Authorization"))
		mcp.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), edgeBearerKey{}, token)))
	}))
	// The authorization handler owns exactly these protocol routes; the core's /v1/ remains gated by workload/OIDC auth.
	handler := authority.Handler()
	for _, path := range []string{"/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp", "/authorize", "/token", "/revoke", "/register", "/auth/callback"} {
		routes.Handle(path, handler)
	}
	return nil
}
