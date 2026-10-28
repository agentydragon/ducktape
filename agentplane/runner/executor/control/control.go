// Package control implements the local Unix-socket protocol between the Runner and its executor container.
package control

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"time"
)

// Request is one operation on the executor companion's local control socket.
type Request struct {
	Operation   string            `json:"operation"`
	Environment map[string]string `json:"environment,omitempty"`
}

// Status identifies the container PID namespace and whether it currently owns exec-server.
type Status struct {
	Generation string `json:"generation"`
	Owned      bool   `json:"owned"`
	// Error explains a claim refused without starting anything. A refusal leaves this generation idle.
	Error string `json:"error,omitempty"`
}

// Connect sends a request and returns the open connection together with the companion's status.
// A successful claim keeps this connection open as the ownership lease until EOF.
func Connect(ctx context.Context, socket string, request Request) (net.Conn, Status, error) {
	c, err := (&net.Dialer{}).DialContext(ctx, "unix", socket)
	if err != nil {
		return nil, Status{}, err
	}
	stop := context.AfterFunc(ctx, func() { _ = c.Close() })
	defer stop()
	deadline, _ := ctx.Deadline()
	_ = c.SetDeadline(deadline)
	if err := json.NewEncoder(c).Encode(request); err != nil {
		_ = c.Close()
		return nil, Status{}, err
	}
	var status Status
	if err := json.NewDecoder(c).Decode(&status); err != nil {
		_ = c.Close()
		return nil, status, err
	}
	if status.Generation == "" {
		_ = c.Close()
		return nil, status, errors.New("executor companion returned no generation")
	}
	_ = c.SetDeadline(time.Time{})
	return c, status, nil
}
