package actions

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"github.com/google/uuid"
)

// eventsPageSize is the most events one read asks for, which is the most the Action Service returns.
const eventsPageSize = 128

// ActionService is the two reads of the Action Service that the Actions source needs: who submitted an Action
// request, and its events.
//
// Transitional: the Action Service's gRPC client replaces Client behind this interface once the Action Service
// serves `ActionService` over gRPC.
//
// A failure a subscription shows and retries is a *worker.ProviderError.
type ActionService interface {
	// Caller is the ServiceAccount that submitted the Action request.
	Caller(ctx context.Context, requestID uuid.UUID) (store.ServiceAccount, error)
	// Events returns the request's next page of events after afterSequence, in sequence order.
	Events(ctx context.Context, requestID uuid.UUID, afterSequence int64) ([]store.ActionEvent, error)
}

// Client reads the Action Service's workload API over HTTP, authenticating with the service's own projected
// ServiceAccount token. That token is allowed to read every account's requests.
type Client struct {
	base      *url.URL
	tokenFile string
	http      *http.Client
}

// NewClient returns a Client for validated settings.
func NewClient(settings Settings) (*Client, error) {
	if err := settings.Validate(); err != nil {
		return nil, err
	}
	base, err := url.Parse(settings.URL)
	if err != nil {
		return nil, err
	}
	return &Client{
		base:      base,
		tokenFile: settings.TokenFile,
		http: &http.Client{
			Timeout: settings.requestTimeout(),
			// A redirect is a failure to show, not a request to follow with the bearer.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}, nil
}

func (c *Client) Caller(ctx context.Context, requestID uuid.UUID) (store.ServiceAccount, error) {
	var view struct {
		Caller *store.ServiceAccount `json:"caller"`
	}
	if err := c.get(ctx, c.base.JoinPath("v1", "action-requests", requestID.String()), &view); err != nil {
		return store.ServiceAccount{}, err
	}
	if view.Caller == nil {
		return store.ServiceAccount{}, validationFailure()
	}
	return *view.Caller, nil
}

func (c *Client) Events(ctx context.Context, requestID uuid.UUID, afterSequence int64) ([]store.ActionEvent, error) {
	target := c.base.JoinPath("v1", "action-requests", requestID.String(), "events")
	target.RawQuery = url.Values{
		"after_sequence": {strconv.FormatInt(afterSequence, 10)},
		"limit":          {strconv.Itoa(eventsPageSize)},
	}.Encode()
	var events []store.ActionEvent
	if err := c.get(ctx, target, &events); err != nil {
		return nil, err
	}
	return events, nil
}

// get reads target into into. The failures it names, and no others, are *worker.ProviderError; a token file
// that cannot be read is not one, and stops the worker.
func (c *Client) get(ctx context.Context, target *url.URL, into any) error {
	token, err := os.ReadFile(c.tokenFile)
	if err != nil {
		return fmt.Errorf("reading the Action Service token: %w", err)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	response, err := c.http.Do(request)
	if err != nil {
		return transportFailure(ctx, err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode > 299 {
		return statusFailure(response.StatusCode)
	}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return transportFailure(ctx, err)
	}
	if err := json.Unmarshal(body, into); err != nil {
		return validationFailure()
	}
	return nil
}

// The messages are what a subscription shows: a status, or the name of the failure.

func transportFailure(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return err
	}
	return &worker.ProviderError{Kind: store.FailureUnavailable, Message: "TransportError"}
}

func validationFailure() error {
	return &worker.ProviderError{Kind: store.FailureProcessingError, Message: "ValidationError"}
}

func statusFailure(code int) error {
	kind := store.FailureProcessingError
	switch {
	case code == http.StatusTooManyRequests:
		kind = store.FailureRateLimited
	case code == http.StatusUnauthorized || code == http.StatusForbidden || code == http.StatusNotFound:
		kind = store.FailureAccessDenied
	case code >= 500:
		kind = store.FailureUnavailable
	}
	return &worker.ProviderError{Kind: kind, Message: "HTTP " + strconv.Itoa(code)}
}
