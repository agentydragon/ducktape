// Reconstructed from binary: Build ID a6f96673, Go 1.25.6
// Source: internal/api/client.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/client.go

package api

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Client is the interface for the API client. Implemented by HttpClient
// and stdinConfigClient (in cmd package).
// Binary itab: go:itab.*cmd.stdinConfigClient,api.Client at 0xf5a240
type Client interface {
	RetryableHTTPDo(ctx interface{}, req *http.Request) (*http.Response, error)
}

// RetryConfig holds parameters for exponential backoff retry logic.
// Binary type eq: 0x832ee0
type RetryConfig struct {
	MaxRetries    int64         // offset 0x00
	InitialDelay  time.Duration // offset 0x08 (nanoseconds)
	MaxDelay      time.Duration // offset 0x10 (nanoseconds)
	BackoffFactor float64       // offset 0x18
}

// HttpClient is the core HTTP client for the Anthropic environment manager API.
// Binary type eq: 0x832e60
//
// Struct layout (verified via type:.eq at 0x832e60):
//
//	0x00-0x0f: BaseURL  string   (ptr + len, compared via memequal)
//	0x10:      Client   *http.Client
//	0x18:      Logger   *slog.Logger
//	0x20:      Retry    *RetryConfig
type HttpClient struct {
	BaseURL string       // offset 0x00 (string: ptr at 0x00, len at 0x08)
	Client  *http.Client // offset 0x10
	Logger  interface{}  // offset 0x18 (*slog.Logger)
	Retry   *RetryConfig // offset 0x20
}

// NewHttpClient creates a new HttpClient, ensuring the baseURL has a scheme prefix.
// If the URL starts with "http://" (7 chars) or "https://" (8 chars), it is used as-is;
// otherwise "https://" is prepended.
//
// Binary address: 0x82de80
// Calls: NewHttpClientWithOptions with timeout=0, retryConfig=nil
func NewHttpClient(baseURL string, args ...interface{}) *HttpClient {
	if len(baseURL) >= 7 && baseURL[:7] == "http://" {
		// ok
	} else if len(baseURL) >= 8 && baseURL[:8] == "https://" {
		// ok
	} else {
		baseURL = "https://" + baseURL
	}
	var apiKey string
	var logger interface{}
	// Extract apiKey and logger from variadic args based on types
	for _, arg := range args {
		if arg == nil {
			continue
		}
		switch v := arg.(type) {
		case string:
			if apiKey == "" {
				apiKey = v
			}
		default:
			if logger == nil {
				logger = v
			}
		}
	}
	return NewHttpClientWithOptions(baseURL, apiKey, logger, 0, nil)
}

// NewHttpClientWithOptions creates a new HttpClient with configurable timeout and retry.
//
// Default timeout: 5 minutes (0x45d964b800 = 300,000,000,000 ns)
// Default RetryConfig (when nil):
//
//	MaxRetries:    3
//	InitialDelay:  1s  (0x3b9aca00 = 1,000,000,000 ns)
//	MaxDelay:      10s (0x2540be400 = 10,000,000,000 ns)
//	BackoffFactor: 2.0 (0x4000000000000000 IEEE754)
//
// The baseURL is right-trimmed of "/" characters.
// An http.Transport is created with the given timeout, wrapped in an http.Client.
//
// Binary address: 0x82df80
func NewHttpClientWithOptions(baseURL string, apiKey string, logger interface{}, timeout time.Duration, retryConfig *RetryConfig) *HttpClient {
	if timeout == 0 {
		timeout = 5 * time.Minute
	}

	if retryConfig == nil {
		retryConfig = &RetryConfig{
			MaxRetries:    3,
			InitialDelay:  1 * time.Second,
			MaxDelay:      10 * time.Second,
			BackoffFactor: 2.0,
		}
	}

	baseURL = strings.TrimRight(baseURL, "/")

	hc := &HttpClient{}
	// The binary sets a function pointer at offset 0xa8 on the HttpClient struct,
	// likely a custom RoundTripper or CheckRedirect function.

	transport := &http.Transport{
		ResponseHeaderTimeout: timeout,
	}
	httpClient := &http.Client{
		Transport: transport,
	}

	hc.BaseURL = baseURL
	hc.Client = httpClient
	hc.Logger = logger
	hc.Retry = retryConfig

	return hc
}

// setAuthHeader sets the "Authorization" header on the HTTP request to "Bearer <apiKey>".
//
// Binary address: 0x82e160
// String references:
//
//	"Bearer %s" (9 bytes, fmt.Sprintf format)
//	"Authorization" (13 bytes, header key)
func (c *HttpClient) setAuthHeader(req *http.Request, apiKey string) {
	authValue := fmt.Sprintf("Bearer %s", apiKey)
	req.Header.Set("Authorization", authValue)
}
