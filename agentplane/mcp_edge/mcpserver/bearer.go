package mcpserver

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strconv"

	"github.com/modelcontextprotocol/go-sdk/auth"
)

// ErrAuthorityUnavailable is what a TokenVerifier returns, wrapped, when the Connection authority could neither
// accept nor refuse the bearer. The caller is asked to retry instead of being told its token is invalid.
var ErrAuthorityUnavailable = errors.New("connection authority unavailable")

var errVerifierFailed = errors.New("verifying the bearer failed")

// invalidTokenDescription is FastMCP's description of a refused bearer, which MCP clients act on by
// re-authorizing.
const invalidTokenDescription = "Authentication failed. The provided bearer token is invalid, expired, or no longer " +
	"recognized by the server. To resolve: clear authentication tokens in your MCP client and reconnect. Your " +
	"client should automatically re-register and obtain new tokens."

// requireCaller admits a request whose one Authorization header holds a bearer the verifier accepts, through
// auth.RequireBearerToken so the request's context carries its auth.TokenInfo. Refusals are FastMCP's: a request
// without the header gets a bare challenge (RFC 6750 §3.1), any other refused request an invalid_token one.
func requireCaller(verifier auth.TokenVerifier, resourceMetadataURL string, logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		presented := r.Header.Values("Authorization")
		if len(presented) == 0 {
			challenge := "Bearer"
			if resourceMetadataURL != "" {
				challenge += ` resource_metadata="` + resourceMetadataURL + `"`
			}
			w.Header().Set("WWW-Authenticate", challenge)
			w.Header().Set("Content-Length", "0")
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		// Several credentials have not said which one the request stands on.
		if len(presented) > 1 {
			refuseToken(w, resourceMetadataURL)
			return
		}
		unavailable := false
		verify := func(ctx context.Context, token string, request *http.Request) (*auth.TokenInfo, error) {
			info, err := verifier(ctx, token, request)
			switch {
			case err == nil || errors.Is(err, auth.ErrInvalidToken):
				return info, err
			case errors.Is(err, ErrAuthorityUnavailable):
				unavailable = true
			default:
				logger.ErrorContext(ctx, "verifying a caller bearer failed", "error", err)
			}
			return nil, errVerifierFailed
		}
		admitted := false
		refusal := &refusalRecorder{header: http.Header{}}
		// FastMCP accepts a token without an expiry, as a Connection grant's can be.
		gate := auth.RequireBearerToken(verify, &auth.RequireBearerTokenOptions{AllowMissingExpiration: true})
		gate(http.HandlerFunc(func(_ http.ResponseWriter, admittedRequest *http.Request) {
			admitted = true
			next.ServeHTTP(w, admittedRequest)
		})).ServeHTTP(refusal, r)
		switch {
		case admitted:
		case unavailable:
			body := `{"detail":"Connection authorization is temporarily unavailable."}`
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Content-Length", strconv.Itoa(len(body)))
			w.Header().Set("Retry-After", "60")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(body))
		case refusal.status == http.StatusUnauthorized:
			refuseToken(w, resourceMetadataURL)
		default:
			http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		}
	})
}

func refuseToken(w http.ResponseWriter, resourceMetadataURL string) {
	challenge := `Bearer error="invalid_token", error_description="` + invalidTokenDescription + `"`
	if resourceMetadataURL != "" {
		challenge += `, resource_metadata="` + resourceMetadataURL + `"`
	}
	body := pythonDumps.encode(jsonObject{{"error", "invalid_token"}, {"error_description", invalidTokenDescription}})
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.Header().Set("WWW-Authenticate", challenge)
	w.WriteHeader(http.StatusUnauthorized)
	_, _ = w.Write([]byte(body))
}

// refusalRecorder takes what auth.RequireBearerToken writes when it refuses, so the refusal can be answered in
// FastMCP's form instead.
type refusalRecorder struct {
	header http.Header
	status int
}

func (r *refusalRecorder) Header() http.Header { return r.header }

func (r *refusalRecorder) WriteHeader(status int) { r.status = status }

func (r *refusalRecorder) Write(body []byte) (int, error) { return len(body), nil }
