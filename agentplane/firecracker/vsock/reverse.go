package vsock

import (
	"context"
	"net"
	"sync"
	"time"
)

const maxRetryDelay = time.Second

// DialListener adapts outbound stream dialing to net.Listener. It retries
// failed dials with capped backoff and waits for each stream to close.
func DialListener(ctx context.Context, dial func(context.Context) (net.Conn, error)) net.Listener {
	parent := ctx
	ctx, cancel := context.WithCancel(parent)
	l := &dialListener{ctx: ctx, cancel: cancel, dial: dial, done: make(chan struct{})}
	go func() {
		select {
		case <-parent.Done():
			l.shutdown(parent.Err())
		case <-l.done:
		}
	}()
	return l
}

type dialListener struct {
	ctx    context.Context
	cancel context.CancelFunc
	dial   func(context.Context) (net.Conn, error)
	done   chan struct{}
	mu     sync.Mutex
	active *trackedConn
	accept sync.Mutex
	close  sync.Once
	err    error
}

func (l *dialListener) Accept() (net.Conn, error) {
	l.accept.Lock()
	defer l.accept.Unlock()
	var delay time.Duration
	for {
		select {
		case <-l.done:
			return nil, l.acceptError()
		default:
		}
		l.mu.Lock()
		active := l.active
		l.mu.Unlock()
		if active != nil {
			select {
			case <-active.closed:
				l.mu.Lock()
				if l.active == active {
					l.active = nil
				}
				l.mu.Unlock()
				continue
			case <-l.done:
				return nil, net.ErrClosed
			}
		}
		connection, err := l.dial(l.ctx)
		if err != nil {
			if contextErr := l.ctx.Err(); contextErr != nil {
				l.shutdown(contextErr)
				return nil, l.acceptError()
			}
			delay = retryDelay(delay)
			timer := time.NewTimer(delay)
			select {
			case <-timer.C:
			case <-l.done:
				timer.Stop()
				return nil, l.acceptError()
			}
			continue
		}
		l.mu.Lock()
		select {
		case <-l.done:
			l.mu.Unlock()
			_ = connection.Close()
			return nil, l.acceptError()
		case <-l.ctx.Done():
			l.mu.Unlock()
			_ = connection.Close()
			l.shutdown(l.ctx.Err())
			return nil, l.acceptError()
		default:
			active = newTrackedConn(connection)
			l.active = active
		}
		l.mu.Unlock()
		return active, nil
	}
}

func (l *dialListener) Close() error {
	l.shutdown(net.ErrClosed)
	return nil
}

func (l *dialListener) shutdown(err error) {
	l.close.Do(func() {
		l.err = err
		close(l.done)
		l.cancel()
		l.mu.Lock()
		active := l.active
		l.mu.Unlock()
		if active != nil {
			_ = active.Close()
		}
	})
}

func (l *dialListener) acceptError() error {
	if l.err != nil {
		return l.err
	}
	return net.ErrClosed
}

func (l *dialListener) Addr() net.Addr { return reverseAddr{} }

// DialBroker adapts accepted streams to gRPC's context dialer.
type DialBroker struct {
	listener net.Listener
	requests chan dialRequest
	done     chan struct{}
	stopped  chan struct{}
	mu       sync.Mutex
	active   *trackedConn
	close    sync.Once
}

type dialRequest struct {
	ctx    context.Context
	result chan net.Conn
}

// AcceptDialer starts a broker that hands listener streams to DialContext.
func AcceptDialer(listener net.Listener) *DialBroker {
	b := &DialBroker{
		listener: listener,
		requests: make(chan dialRequest, 1),
		done:     make(chan struct{}),
		stopped:  make(chan struct{}),
	}
	go b.serve()
	return b
}

// DialContext returns the next accepted stream; target is ignored.
func (b *DialBroker) DialContext(ctx context.Context, _ string) (net.Conn, error) {
	r := dialRequest{ctx: ctx, result: make(chan net.Conn)}
	select {
	case b.requests <- r:
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-b.done:
		return nil, net.ErrClosed
	}
	select {
	case conn := <-r.result:
		if err := ctx.Err(); err != nil {
			_ = conn.Close()
			return nil, err
		}
		select {
		case <-b.done:
			_ = conn.Close()
			return nil, net.ErrClosed
		default:
			return conn, nil
		}
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-b.done:
		return nil, net.ErrClosed
	}
}

// Close stops accepting, closes the active stream, and joins the broker goroutine.
func (b *DialBroker) Close() error {
	b.stop()
	<-b.stopped
	return nil
}

func (b *DialBroker) serve() {
	defer close(b.stopped)
	for {
		conn, err := b.listener.Accept()
		if err != nil {
			b.stop()
			return
		}
		tracked := newTrackedConn(conn)
		if !b.dispatch(tracked) {
			return
		}
		select {
		case <-tracked.closed:
		case <-b.done:
			return
		}
	}
}

func (b *DialBroker) dispatch(conn *trackedConn) bool {
	for {
		select {
		case r := <-b.requests:
			if r.ctx.Err() != nil {
				_ = conn.Close()
				return true
			}
			b.mu.Lock()
			b.active = conn
			b.mu.Unlock()
			select {
			case r.result <- conn:
				return true
			case <-r.ctx.Done():
				_ = conn.Close()
				return true
			case <-b.done:
				_ = conn.Close()
				return false
			}
		case <-b.done:
			_ = conn.Close()
			return false
		}
	}
}

func (b *DialBroker) stop() {
	b.close.Do(func() {
		close(b.done)
		b.mu.Lock()
		active := b.active
		b.mu.Unlock()
		_ = b.listener.Close()
		if active != nil {
			_ = active.Close()
		}
	})
}

type trackedConn struct {
	net.Conn
	closed chan struct{}
	close  sync.Once
}

func newTrackedConn(conn net.Conn) *trackedConn {
	return &trackedConn{Conn: conn, closed: make(chan struct{})}
}

func (c *trackedConn) Read(p []byte) (int, error) {
	n, err := c.Conn.Read(p)
	if err != nil {
		_ = c.Close()
	}
	return n, err
}

func (c *trackedConn) Close() error {
	var err error
	c.close.Do(func() {
		err = c.Conn.Close()
		close(c.closed)
	})
	return err
}

func retryDelay(current time.Duration) time.Duration {
	if current == 0 {
		return 10 * time.Millisecond
	}
	if current >= maxRetryDelay/2 {
		return maxRetryDelay
	}
	return current * 2
}

type reverseAddr struct{}

func (reverseAddr) Network() string { return "vsock" }
func (reverseAddr) String() string  { return "firecracker-reverse" }
