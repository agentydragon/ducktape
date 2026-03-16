package auth

import (
	"context"
	"fmt"
	"log/slog"
	"net/url"
	"strings"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y/diag"
)

// Permission represents a source permission type.
type Permission string

// SourceAuthProvider is the interface for source authentication providers.
type SourceAuthProvider interface {
	AuthenticateURL(
		ctx context.Context,
		authCtx *AuthContext,
		sourceURL string,
		permission Permission,
	) (string, bool)
	SanitizeOutput(
		output string,
		gitURL string,
		authenticatedURL string,
	) string
}

// GitHubAuthType represents the type of GitHub authentication.
type GitHubAuthType int

const (
	// GitHubAuthTypeDirect uses the token directly as a GitHub personal access token.
	GitHubAuthTypeDirect GitHubAuthType = 0
	// GitHubAuthTypeGitProxy uses the token as a git proxy JWT, wrapped with "unused:" prefix.
	GitHubAuthTypeGitProxy GitHubAuthType = 1
)

// GitHubSourceAuthProvider implements SourceAuthProvider for GitHub repositories.
type GitHubSourceAuthProvider struct {
	authType GitHubAuthType
	token    string
	logger   *slog.Logger
}

// NewGitHubSourceAuthProvider creates a new GitHubSourceAuthProvider.
// If the token starts with "sk-ant-ccsr-", the auth type is set to
// GitHubAuthTypeGitProxy; otherwise it is GitHubAuthTypeDirect.
func NewGitHubSourceAuthProvider(
	logger *slog.Logger,
	token string,
) SourceAuthProvider {
	if strings.HasPrefix(token, "sk-ant-ccsr-") {
		logger.Log(context.Background(), slog.LevelInfo,
			"GitHub source auth provider initialized with git proxy JWT",
		)
		return &GitHubSourceAuthProvider{
			authType: GitHubAuthTypeGitProxy,
			token:    token,
			logger:   logger,
		}
	}

	return &GitHubSourceAuthProvider{
		authType: GitHubAuthTypeDirect,
		token:    token,
		logger:   logger,
	}
}

// AuthenticateURL applies GitHub authentication credentials to the given
// source URL. It returns the (possibly modified) URL and a boolean indicating
// whether authentication was applied.
func (g *GitHubSourceAuthProvider) AuthenticateURL(
	ctx context.Context,
	authCtx *AuthContext,
	sourceURL string,
	permission Permission,
) (string, bool) {
	isSSH := strings.HasPrefix(sourceURL, "git@") || strings.HasPrefix(sourceURL, "ssh://")
	if isSSH {
		g.logger.Log(ctx, slog.LevelDebug,
			"SSH URL detected, cannot use token authentication",
			"url", sourceURL,
		)
		return sourceURL, false
	}

	u, err := url.Parse(sourceURL)
	if err != nil {
		diag.LogEnvManagerNoPII(ctx, "github_url_parse_failed", nil)
		g.logger.Log(ctx, slog.LevelWarn,
			"Failed to parse GitHub URL",
			"url", sourceURL,
			"error", err,
		)
		return sourceURL, false
	}

	if u.Scheme != "https" {
		return sourceURL, false
	}

	var authToken string

	switch g.authType {
	case GitHubAuthTypeGitProxy:
		if g.token == "" {
			diag.LogEnvManagerNoPII(ctx, "git_proxy_jwt_unavailable", nil)
			g.logger.Log(ctx, slog.LevelWarn,
				"No git proxy JWT available",
			)
			return sourceURL, false
		}
		authToken = fmt.Sprintf("unused:%s", g.token)
		g.logger.Log(ctx, slog.LevelInfo,
			"Using git proxy authentication",
			"url", sourceURL,
		)

	case GitHubAuthTypeDirect:
		if !strings.Contains(sourceURL, "github.com") {
			return sourceURL, false
		}
		if g.token == "" {
			return sourceURL, false
		}
		authToken = g.token

	default:
		return sourceURL, false
	}

	if strings.Contains(authToken, ":") {
		parts := strings.SplitN(authToken, ":", 2)
		u.User = url.UserPassword(parts[0], parts[1])
	} else {
		u.User = url.User(authToken)
	}

	authenticatedURL := u.String()

	g.logger.Log(ctx, slog.LevelDebug,
		"GitHub authentication applied",
		"original_url", sourceURL,
		"auth_type", g.authType,
		"permission", string(permission),
		"authenticated", true,
	)

	return authenticatedURL, true
}

// SanitizeOutput removes authentication credentials from command output.
// It replaces the authenticated URL with the original git URL and masks
// the token with "***".
func (g *GitHubSourceAuthProvider) SanitizeOutput(
	output string,
	gitURL string,
	authenticatedURL string,
) string {
	if gitURL != authenticatedURL && authenticatedURL != "" {
		output = strings.Replace(output, authenticatedURL, gitURL, -1)
	}

	if g.token != "" {
		output = strings.Replace(output, g.token, "***", -1)
		output = strings.Replace(output, "unused:"+g.token, "unused:***", -1)
	}

	return output
}
