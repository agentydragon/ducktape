package adapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"

	"github.com/coder/websocket"
)

// ProbeAt checks the Codex exec-server dispatcher with a deadline on its handshake and response.
func ProbeAt(endpoint string) func(context.Context) error {
	return func(ctx context.Context) error { return probe(ctx, endpoint) }
}

func probe(ctx context.Context, endpoint string) error {
	connection, _, err := websocket.Dial(ctx, endpoint, &websocket.DialOptions{HTTPClient: &http.Client{Transport: &http.Transport{Proxy: nil}}})
	if err != nil {
		return fmt.Errorf("executor WebSocket dial: %w", err)
	}
	defer connection.CloseNow()
	connection.SetReadLimit(65536)
	if err := connection.Write(ctx, websocket.MessageText, []byte(`{"id":1,"method":"initialize","params":{"clientName":"agentplane-health"}}`)); err != nil {
		return fmt.Errorf("executor initialize write: %w", err)
	}
	_, data, err := connection.Read(ctx)
	if err != nil {
		return fmt.Errorf("executor initialize read: %w", err)
	}
	var response struct {
		ID     int `json:"id"`
		Result struct {
			SessionID string `json:"sessionId"`
		} `json:"result"`
	}
	if err := json.Unmarshal(data, &response); err != nil {
		return fmt.Errorf("executor initialize decode: %w", err)
	}
	if response.ID != 1 || response.Result.SessionID == "" {
		return errors.New("executor health initialize returned no session")
	}
	if err := connection.Write(ctx, websocket.MessageText, []byte(`{"method":"initialized","params":null}`)); err != nil {
		return fmt.Errorf("executor initialized write: %w", err)
	}
	if err := connection.Write(ctx, websocket.MessageText, []byte(`{"id":2,"method":"environment/status","params":null}`)); err != nil {
		return fmt.Errorf("executor environment/status write: %w", err)
	}
	_, data, err = connection.Read(ctx)
	if err != nil {
		return fmt.Errorf("executor environment/status read: %w", err)
	}
	var status struct {
		ID     int `json:"id"`
		Result struct {
			Status string `json:"status"`
		} `json:"result"`
	}
	if err := json.Unmarshal(data, &status); err != nil {
		return fmt.Errorf("executor environment/status decode: %w", err)
	}
	if status.ID != 2 || status.Result.Status != "ready" {
		return fmt.Errorf("executor environment/status returned id=%d status=%q, expected id=2 status=ready", status.ID, status.Result.Status)
	}
	return nil
}
