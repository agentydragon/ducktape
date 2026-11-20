package codexshim

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/coder/websocket"
)

var deniedHeaderEnv = []string{"CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN", "OPENAI_FEDERATION_RULE_ID", "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_WORKLOAD_IDENTITY_CONTEXT", "OPENAI_API_KEY", "CODEX_GUARDIAN_DECISIONS_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_CONNECTORS_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_FEDERATED_TOKEN_FILE", "GOOGLE_APPLICATION_CREDENTIALS"}

func openHTTPRequest(ctx context.Context, p params) (*http.Response, context.CancelFunc, error) {
	u, err := url.Parse(p.HTTPURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, nil, errors.New("expected absolute HTTP URL")
	}
	var cancel context.CancelFunc
	if p.TimeoutMS != nil {
		if *p.TimeoutMS < 0 {
			return nil, nil, errors.New("invalid HTTP timeout")
		}
		ctx, cancel = context.WithTimeout(ctx, time.Duration(*p.TimeoutMS)*time.Millisecond)
	} else {
		ctx, cancel = context.WithCancel(ctx)
	}
	req, err := http.NewRequestWithContext(ctx, p.HTTPMethod, p.HTTPURL, bytes.NewReader(p.BodyBase64))
	if err != nil {
		cancel()
		return nil, nil, err
	}
	for _, h := range p.Headers {
		value := h.Value
		if h.ValueEnvVar != nil {
			for _, denied := range deniedHeaderEnv {
				if strings.EqualFold(denied, *h.ValueEnvVar) {
					cancel()
					return nil, nil, errors.New("HTTP header environment variable is not allowed")
				}
			}
			v, ok := os.LookupEnv(*h.ValueEnvVar)
			if !ok || v == "" {
				cancel()
				return nil, nil, errors.New("HTTP header environment variable is missing or empty")
			}
			value += v
		}
		req.Header.Add(h.Name, value)
	}
	client := &http.Client{}
	switch p.RedirectPolicy {
	case "", "follow":
	case "stop":
		client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	default:
		cancel()
		return nil, nil, errors.New("unsupported redirect policy")
	}
	resp, err := client.Do(req)
	if err != nil {
		cancel()
		return nil, nil, err
	}
	return resp, cancel, nil
}

func responseHeaders(resp *http.Response) []map[string]string {
	headers := []map[string]string{}
	for name, values := range resp.Header {
		for _, value := range values {
			headers = append(headers, map[string]string{"name": name, "value": value})
		}
	}
	return headers
}

func httpRequest(ctx context.Context, p params) (any, error) {
	if p.StreamResponse {
		return nil, errors.New("streamed response requires connection")
	}
	resp, cancel, err := openHTTPRequest(ctx, p)
	if err != nil {
		return nil, err
	}
	defer cancel()
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20+1))
	if err != nil {
		return nil, err
	}
	if len(body) > 8<<20 {
		return nil, fmt.Errorf("HTTP response exceeds 8 MiB")
	}
	return map[string]any{"status": resp.StatusCode, "headers": responseHeaders(resp), "bodyBase64": body}, nil
}

func streamHTTP(ctx context.Context, ws *websocket.Conn, q request, p params) {
	resp, cancel, err := openHTTPRequest(ctx, p)
	if err != nil {
		reply(ctx, ws, q.ID, nil, err)
		return
	}
	defer cancel()
	defer resp.Body.Close()
	reply(ctx, ws, q.ID, map[string]any{"status": resp.StatusCode, "headers": responseHeaders(resp), "bodyBase64": []byte{}}, nil)
	buf := make([]byte, 64<<10)
	seq := uint64(1)
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			b, _ := json.Marshal(map[string]any{"method": "http/request/bodyDelta", "params": map[string]any{"requestId": p.HTTPRequestID, "seq": seq, "deltaBase64": buf[:n], "done": false, "error": nil}})
			if ws.Write(ctx, websocket.MessageText, b) != nil {
				return
			}
			seq++
		}
		if readErr != nil {
			var message any
			if !errors.Is(readErr, io.EOF) {
				message = readErr.Error()
			}
			b, _ := json.Marshal(map[string]any{"method": "http/request/bodyDelta", "params": map[string]any{"requestId": p.HTTPRequestID, "seq": seq, "deltaBase64": []byte{}, "done": true, "error": message}})
			_ = ws.Write(ctx, websocket.MessageText, b)
			return
		}
	}
}
