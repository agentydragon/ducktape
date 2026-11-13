package proxy

import (
	"context"
	"maps"
	"net"
	"net/http"
	"slices"
	"sync"

	"golang.org/x/sync/errgroup"
)

// flights counts the requests in flight and lets a drain wait for them.
type flights struct {
	mu sync.Mutex
	n  int
	// idle is closed exactly while no request is in flight.
	idle chan struct{}
}

func newFlights() *flights {
	idle := make(chan struct{})
	close(idle)
	return &flights{idle: idle}
}

// begin counts a request in flight until the returned function is called.
func (f *flights) begin() (end func()) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.n == 0 {
		f.idle = make(chan struct{})
	}
	f.n++
	return func() {
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.n--; f.n == 0 {
			close(f.idle)
		}
	}
}

// wait returns once no request is in flight, or ctx ends.
func (f *flights) wait(ctx context.Context) error {
	f.mu.Lock()
	idle := f.idle
	f.mu.Unlock()
	select {
	case <-idle:
		return nil
	case <-ctx.Done():
		return context.Cause(ctx)
	}
}

// tunnels holds the connections hijacked for CONNECT, which the outer http.Server no longer
// tracks, so a drain can wind them down and close them.
type tunnels struct {
	mu sync.Mutex
	// open maps each tunnel to the server of the requests inside it, nil until it is being served.
	open   map[net.Conn]*http.Server
	closed bool
}

// add registers conn, or reports false when the tunnels have already been closed.
func (t *tunnels) add(conn net.Conn) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return false
	}
	if t.open == nil {
		t.open = map[net.Conn]*http.Server{}
	}
	t.open[conn] = nil
	return true
}

// serving records the server of the requests inside a registered tunnel.
func (t *tunnels) serving(conn net.Conn, server *http.Server) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if _, registered := t.open[conn]; registered {
		t.open[conn] = server
	}
}

func (t *tunnels) remove(conn net.Conn) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.open, conn)
}

// shutdown has every tunnel stop taking new requests and finish writing the responses it owes.
func (t *tunnels) shutdown(ctx context.Context) error {
	t.mu.Lock()
	servers := slices.DeleteFunc(slices.Collect(maps.Values(t.open)), func(s *http.Server) bool { return s == nil })
	t.mu.Unlock()
	var group errgroup.Group
	for _, server := range servers {
		group.Go(func() error { return server.Shutdown(ctx) })
	}
	return group.Wait()
}

// closeAll closes every tunnel and refuses any added after.
func (t *tunnels) closeAll() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.closed = true
	for conn := range t.open {
		conn.Close()
	}
}
