package input

import (
	"encoding/base64"
	"encoding/json"
	"fmt"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
)

// WorkSecret represents the decoded work secret containing sources.
type WorkSecret struct {
	Version int             `json:"version"`
	Sources []config.Source `json:"-"`

	// rawSources holds the intermediate unmarshaled source entries.
	rawSources []rawSource
}

type rawSource struct {
	Type    string          `json:"type"`
	Content json.RawMessage `json:"content"`
}

// UnmarshalJSON implements custom JSON unmarshaling for WorkSecret.
// It decodes the version and sources, handling the "git_repository" source type.
func (ws *WorkSecret) UnmarshalJSON(data []byte) error {
	var raw struct {
		Version    int         `json:"version"`
		RawSources []rawSource `json:"sources"`
	}

	if err := json.Unmarshal(data, &raw); err != nil {
		return fmt.Errorf("failed to unmarshal work secret: %w", err)
	}

	ws.Version = raw.Version
	ws.Sources = make([]config.Source, 0, len(raw.RawSources))

	for i, s := range raw.RawSources {
		switch s.Type {
		case "git_repository":
			var repo config.GitRepositorySource
			if err := json.Unmarshal(s.Content, &repo); err != nil {
				return fmt.Errorf("failed to parse git repository source[%d]: %w", i, err)
			}
			ws.Sources = append(ws.Sources, &repo)
		default:
			return fmt.Errorf("unknown source type[%d]: %s", i, s.Type)
		}
	}

	return nil
}

// DecodeWorkSecret decodes a base64-encoded work secret string into a WorkSecret.
func DecodeWorkSecret(secret string) (*WorkSecret, error) {
	if secret == "" {
		return nil, fmt.Errorf("secret is empty")
	}

	decoded, err := base64.URLEncoding.DecodeString(secret)
	if err != nil {
		return nil, fmt.Errorf("invalid secret")
	}

	var ws WorkSecret
	if err := json.Unmarshal(decoded, &ws); err != nil {
		return nil, fmt.Errorf("invalid secret")
	}

	if ws.Version != 1 {
		return nil, fmt.Errorf("unsupported secret version. Download the latest version of the Claude Environment Runner")
	}

	return &ws, nil
}

// V1WorkResponse represents the parsed V1 work response from the API.
type V1WorkResponse struct {
	ID   string     `json:"id"`
	Type string     `json:"type"`
	Data V1WorkData `json:"data"`
}

// V1WorkData contains the data portion of a V1 work response.
type V1WorkData struct {
	ID        string `json:"id"`
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	Secret    string `json:"secret"`
}

// GetSessionID returns the session ID from the work response data.
func (r *V1WorkResponse) GetSessionID() string {
	return r.Data.SessionID
}

// IsHealthcheck returns true if the work response data type is "healthcheck".
func (r *V1WorkResponse) IsHealthcheck() bool {
	return r.Data.Type == "healthcheck"
}

// DecodeSecret decodes the work secret from the response's data secret field.
func (r *V1WorkResponse) DecodeSecret() (*WorkSecret, error) {
	return DecodeWorkSecret(r.Data.Secret)
}

// ParseV1WorkResponse parses raw JSON bytes into a V1WorkResponse.
// It validates that the response type is "work" and data type is "session" or "healthcheck".
func ParseV1WorkResponse(data []byte) (*V1WorkResponse, error) {
	var resp V1WorkResponse
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, fmt.Errorf("failed to parse V1 work response: %w", err)
	}

	if resp.Type != "work" {
		return nil, fmt.Errorf("unexpected type %q (expected 'work')", resp.Type)
	}

	switch resp.Data.Type {
	case "session":
		if resp.Data.SessionID == "" {
			return nil, fmt.Errorf("missing session ID in data.id")
		}
	case "healthcheck":
		// healthcheck is valid without session ID
	default:
		return nil, fmt.Errorf("unexpected data.type %q (expected 'session' or 'healthcheck')", resp.Data.Type)
	}

	return &resp, nil
}
