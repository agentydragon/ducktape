package providerio

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

// maxErrorBodyBytes bounds the non-2xx response body kept as failure evidence.
const maxErrorBodyBytes = 1024 * 1024

// Request is one streaming provider POST.
type Request struct {
	URL  string
	Body []byte
	// Headers apply in order: a later layer overrides an earlier one, and an empty value unsets the header.
	Headers []map[string]string
	// Provider names the API in status errors.
	Provider string
	// RequestIDHeader names the response header carrying the provider's request ID.
	RequestIDHeader string
}

// Response is a 2xx provider response. Body is bounded by protocol.MaxProviderStreamBytes and
// records the transport bytes it reads as evidence.
type Response struct {
	Head protocol.HTTPResponseHead
	Body io.Reader
	body io.Closer
}

func (r *Response) Close() error { return r.body.Close() }

// Post sends request and records the response head as evidence before firing
// hooks.ResponseHeadRecorded. A non-2xx status is returned as ProviderStatusError carrying the
// head and up to maxErrorBodyBytes of the body.
func Post(ctx context.Context, client *http.Client, request Request, evidence protocol.EvidenceSink, hooks protocol.ProviderHooks) (*Response, error) {
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, request.URL, bytes.NewReader(request.Body))
	if err != nil {
		return nil, err
	}
	for _, layer := range request.Headers {
		for name, value := range layer {
			if value == "" {
				httpRequest.Header.Del(name)
			} else {
				httpRequest.Header.Set(name, value)
			}
		}
	}
	response, err := client.Do(httpRequest)
	if err != nil {
		return nil, ProviderTransportError(ctx, err)
	}
	head := protocol.HTTPResponseHead{
		HTTPStatus: response.StatusCode, ProviderRequestID: response.Header.Get(request.RequestIDHeader), ContentType: response.Header.Get("content-type"),
	}
	if err := protocol.RecordProviderEvidence(ctx, evidence, head, false); err != nil {
		response.Body.Close()
		return nil, err
	}
	if hooks.ResponseHeadRecorded != nil {
		hooks.ResponseHeadRecorded()
	}
	reader := io.LimitReader(protocol.CaptureReader{Reader: response.Body, Context: ctx, Evidence: evidence}, protocol.MaxProviderStreamBytes)
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		defer response.Body.Close()
		body, err := io.ReadAll(io.LimitReader(reader, maxErrorBodyBytes+1))
		if err != nil {
			return nil, ProviderTransportError(ctx, err)
		}
		if len(body) > maxErrorBodyBytes {
			body = append(body[:maxErrorBodyBytes], "[truncated]"...)
		}
		return nil, ProviderStatusError(response, request.Provider, map[string]any{
			"http_status": head.HTTPStatus, "provider_request_id": head.ProviderRequestID, "content_type": head.ContentType, "body": string(body),
		})
	}
	return &Response{Head: head, Body: reader, body: response.Body}, nil
}

// ReadSSE calls dispatch with each server-sent event's data (its data lines joined by "\n") until
// dispatch returns an error, which it returns after ProviderContextError, or the stream ends,
// when it returns nil. A read failure is returned as ProviderTransportError.
func ReadSSE(ctx context.Context, reader io.Reader, dispatch func(data string) error) error {
	lineReader := bufio.NewReader(reader)
	var lines []string
	for {
		line, readErr := lineReader.ReadString('\n')
		if len(line) > 0 {
			line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
			if value, ok := strings.CutPrefix(line, "data:"); ok {
				// The SSE field value drops exactly one leading space.
				lines = append(lines, strings.TrimPrefix(value, " "))
			} else if line == "" && len(lines) > 0 {
				data := strings.Join(lines, "\n")
				lines = nil
				if err := dispatch(data); err != nil {
					return ProviderContextError(ctx, err)
				}
			}
		}
		if errors.Is(readErr, io.EOF) {
			return nil
		}
		if readErr != nil {
			return ProviderTransportError(ctx, readErr)
		}
	}
}
