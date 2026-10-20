package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"strings"
	"sync/atomic"
)

// httpClient uses the default transport, so HTTPS_PROXY, HTTP_PROXY, NO_PROXY and SSL_CERT_FILE
// behave as they do for curl. It follows no redirect and has no way to skip verification.
var httpClient = &http.Client{
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

// service is one configured service: the base URL and the placeholder credential it is called with.
type service struct {
	baseURL    string
	credential string
}

// request is one call. A 409 carries the gRPC code in conflict.
type request struct {
	method   string
	path     string
	query    url.Values
	body     any
	conflict string
}

// connect reads the group's configuration from the environment. Only that group's variables are
// consulted, so a group is usable whatever the others lack.
func (a *app) connect(group string) (*service, error) {
	urlVariable, credentialVariable := "AGENTPLANE_"+group+"_URL", "AGENTPLANE_"+group+"_CREDENTIAL"
	base, credential := a.getenv(urlVariable), a.getenv(credentialVariable)
	var missing []string
	if base == "" {
		missing = append(missing, urlVariable)
	}
	if credential == "" {
		missing = append(missing, credentialVariable)
	}
	if len(missing) > 0 {
		return nil, usageFailure("%s not set", strings.Join(missing, " and "))
	}
	parsed, err := url.Parse(base)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" ||
		parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, usageFailure("%s must be an http(s) base URL without credentials, query or fragment", urlVariable)
	}
	return &service{baseURL: strings.TrimRight(base, "/"), credential: credential}, nil
}

// do makes one call and returns the body of its 2xx answer. Any other outcome is a *failure, or
// errInterrupted when a signal ended the call.
func (s *service) do(ctx context.Context, r request) ([]byte, error) {
	var body io.Reader
	if r.body != nil {
		encoded, err := json.Marshal(r.body)
		if err != nil {
			return nil, clientFailure(exitUsage, "INVALID_ARGUMENT", "request body: %v", err)
		}
		body = bytes.NewReader(encoded)
	}
	target := s.baseURL + r.path
	if len(r.query) > 0 {
		target += "?" + r.query.Encode()
	}
	var sent atomic.Bool
	trace := &httptrace.ClientTrace{WroteRequest: func(info httptrace.WroteRequestInfo) {
		if info.Err == nil {
			sent.Store(true)
		}
	}}
	req, err := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), r.method, target, body)
	if err != nil {
		return nil, clientFailure(exitUsage, "INVALID_ARGUMENT", "request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+s.credential)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	response, err := httpClient.Do(req)
	if err != nil {
		return nil, transportFailure(ctx, err, sent.Load())
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, transportFailure(ctx, err, true)
	}
	if response.StatusCode < 200 || response.StatusCode > 299 {
		conflict := r.conflict
		if conflict == "" {
			conflict = "ABORTED"
		}
		return nil, httpFailure(response.StatusCode, response.Header, payload, conflict)
	}
	return payload, nil
}

func transportFailure(ctx context.Context, err error, sent bool) error {
	if errors.Is(ctx.Err(), context.Canceled) {
		return errInterrupted
	}
	code := "UNAVAILABLE"
	var network net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &network) && network.Timeout()) {
		code = "DEADLINE_EXCEEDED"
	}
	result := clientFailure(exitTransient, code, "%v", err)
	result.sent = sent
	return result
}
