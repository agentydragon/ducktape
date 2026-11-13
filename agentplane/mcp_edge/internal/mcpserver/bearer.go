package mcpserver

import (
	"context"
	"errors"
	"log/slog"
	"net/http"

	"github.com/modelcontextprotocol/go-sdk/auth"
)

// ErrAuthorityUnavailable is what a TokenVerifier returns, wrapped, when the Connection authority could neither
// accept nor refuse the bearer. The caller is asked to retry instead of being told its token is invalid.
var ErrAuthorityUnavailable = errors.New("connection authority unavailable")

var (
	errAuthorityDown  = errors.New("Connection authorization is temporarily unavailable.")
	errVerifierFailed = errors.New("verifying the bearer failed")
)

// requireCaller admits a request whose bearer the verifier accepts, through auth.RequireBearerToken, so the request's
// context carries its auth.TokenInfo. It adds what the middleware cannot: an authority outage answers 503 with a
// Retry-After, and a verifier fault answers 500 without the fault's text.
func requireCaller(verifier auth.TokenVerifier, resourceMetadataURL string, logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Two credentials leave open which one the request stands on; a proxy reading the other one would admit a
		// different caller than this check does. Several count as none.
		if len(r.Header.Values("Authorization")) > 1 {
			r = r.Clone(r.Context())
			r.Header.Del("Authorization")
		}
		outage := &outageWriter{ResponseWriter: w}
		verify := func(ctx context.Context, token string, request *http.Request) (*auth.TokenInfo, error) {
			info, err := verifier(ctx, token, request)
			switch {
			case err == nil, errors.Is(err, auth.ErrInvalidToken):
				return info, err
			case errors.Is(err, ErrAuthorityUnavailable):
				logger.WarnContext(ctx, "the Connection authority is unavailable", "error", err)
				outage.unavailable = true
				return nil, errAuthorityDown
			default:
				logger.ErrorContext(ctx, "verifying a caller bearer failed", "error", err)
				return nil, errVerifierFailed
			}
		}
		// A Connection grant's token need not expire.
		auth.RequireBearerToken(verify, &auth.RequireBearerTokenOptions{
			ResourceMetadataURL:    resourceMetadataURL,
			AllowMissingExpiration: true,
		})(next).ServeHTTP(outage, r)
	})
}

// outageWriter turns the 500 auth.RequireBearerToken answers for a failed verification into a 503 the caller
// retries, when the failure was an authority outage.
type outageWriter struct {
	http.ResponseWriter
	unavailable bool
}

func (w *outageWriter) WriteHeader(status int) {
	if w.unavailable && status == http.StatusInternalServerError {
		w.Header().Set("Retry-After", "60")
		status = http.StatusServiceUnavailable
	}
	w.ResponseWriter.WriteHeader(status)
}

// Unwrap lets http.ResponseController reach the connection's writer, which streams the SSE answers.
func (w *outageWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }
