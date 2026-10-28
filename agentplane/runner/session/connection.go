package session

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
)

// nativeConnection owns protocol readers and handshake waiters. Session supplies
// journal translation and lifecycle observations; the connection never admits work.
type nativeConnection struct {
	deps    nativeConnectionDependencies
	mu      sync.Mutex
	readers sync.WaitGroup
	waiters []*nativeWaiter
	replies []matchedReply
}

type nativeConnectionDependencies struct {
	ctx        context.Context
	journal    *journal.Journal
	send       func(context.Context, json.RawMessage) error
	afterBatch func()
	stderr     func(context.Context, string) error
	ended      func(context.Context, int) error
	fail       func(error)
}

type nativeWaiter struct {
	matches func(json.RawMessage) bool
	result  chan nativeResult
}
type nativeResult struct {
	receipt harness.NativeReceipt
	err     error
}
type matchedReply struct {
	waiter  *nativeWaiter
	receipt harness.NativeReceipt
}

func (c *nativeConnection) startStdout(process *supervision.ManagedProcess, receive func(context.Context, []byte) error) {
	c.readers.Add(1)
	go c.readStdout(process, receive)
}

func (c *nativeConnection) startStderr(process *supervision.ManagedProcess) {
	c.readers.Add(1)
	go c.readStderr(process)
}

func (c *nativeConnection) join() { c.readers.Wait() }

func (c *nativeConnection) receiveBatch(ctx context.Context, lines [][]byte, receive func(context.Context, []byte) error) error {
	if err := c.deps.journal.WithBatch(ctx, func(ctx context.Context) error {
		for _, line := range lines {
			if err := receive(ctx, line); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		return err
	}
	c.deps.afterBatch()
	c.deliverReplies()
	return nil
}

func (c *nativeConnection) request(ctx context.Context, frame json.RawMessage, match func(json.RawMessage) bool) (harness.NativeReceipt, error) {
	if err := ctx.Err(); err != nil {
		return harness.NativeReceipt{}, err
	}
	waiter := &nativeWaiter{matches: match, result: make(chan nativeResult, 1)}
	c.mu.Lock()
	c.waiters = append(c.waiters, waiter)
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		defer c.mu.Unlock()
		for i, w := range c.waiters {
			if w == waiter {
				c.waiters = append(c.waiters[:i], c.waiters[i+1:]...)
				break
			}
		}
	}()
	// Preparation cancellation bounds native I/O and waiting, while its journal remains runner-owned.
	if err := c.deps.send(ctx, frame); err != nil {
		return harness.NativeReceipt{}, err
	}
	select {
	case reply := <-waiter.result:
		return reply.receipt, reply.err
	case <-ctx.Done():
		return harness.NativeReceipt{}, &harness.NativeResponsePendingError{Cause: ctx.Err()}
	}
}

func (c *nativeConnection) match(receipt harness.NativeReceipt) {
	c.mu.Lock()
	waiting := c.waiters[:0]
	for _, w := range c.waiters {
		if w.matches(receipt.Frame) {
			c.replies = append(c.replies, matchedReply{w, receipt})
		} else {
			waiting = append(waiting, w)
		}
	}
	c.waiters = waiting
	c.mu.Unlock()
}

// Exit also releases matches whose frame batch did not commit. Such replies must
// never be reported as successful native receipts.
func (c *nativeConnection) end(code int) {
	c.mu.Lock()
	waiting := c.waiters
	c.waiters = nil
	for _, reply := range c.replies {
		waiting = append(waiting, reply.waiter)
	}
	c.replies = nil
	c.mu.Unlock()
	for _, waiter := range waiting {
		waiter.result <- nativeResult{err: fmt.Errorf("the harness exited with exit_code=%d", code)}
	}
}

func (c *nativeConnection) deliverReplies() {
	c.mu.Lock()
	replies := c.replies
	c.replies = nil
	c.mu.Unlock()
	for _, reply := range replies {
		reply.waiter.result <- nativeResult{receipt: reply.receipt}
	}
}

func (c *nativeConnection) readStdout(process *supervision.ManagedProcess, receive func(context.Context, []byte) error) {
	defer c.readers.Done()
	buffer := make([]byte, 64*1024)
	var pending []byte
	var readErr error
	for {
		n, err := process.Stdout().Read(buffer)
		if n > 0 {
			pending = append(pending, buffer[:n]...)
			if e := c.deps.journal.WithBatch(c.deps.ctx, func(ctx context.Context) error {
				for {
					i := bytes.IndexByte(pending, '\n')
					if i < 0 {
						break
					}
					if i > harness.MaxFrameLineBytes {
						return errors.New("native frame exceeded the 64 MiB limit")
					}
					line := append([]byte(nil), bytes.TrimRight(pending[:i], "\r")...)
					pending = pending[i+1:]
					if e := receive(ctx, line); e != nil {
						return e
					}
				}
				return nil
			}); e != nil {
				readErr = e
				break
			}
			c.deps.afterBatch()
			c.deliverReplies()
			if len(pending) > harness.MaxFrameLineBytes {
				readErr = errors.New("native frame exceeded the 64 MiB limit")
				break
			}
		}
		if err != nil {
			if err == io.EOF && len(pending) > 0 {
				if e := c.deps.journal.WithBatch(c.deps.ctx, func(ctx context.Context) error { return receive(ctx, bytes.TrimRight(pending, "\r")) }); e != nil {
					readErr = e
				} else {
					c.deps.afterBatch()
					c.deliverReplies()
				}
			}
			if err != io.EOF {
				readErr = err
			}
			break
		}
	}
	if readErr != nil {
		c.deps.fail(readErr)
		if err := process.Stop(context.Background()); err != nil {
			c.deps.fail(err)
		}
	}
	code, err := process.Wait(context.Background())
	if err != nil {
		c.deps.fail(err)
	}
	if err := c.deps.ended(c.deps.ctx, code); err != nil {
		c.deps.fail(err)
	}
}

func (c *nativeConnection) readStderr(process *supervision.ManagedProcess) {
	defer c.readers.Done()
	buffer := make([]byte, 4096)
	for {
		n, err := process.Stderr().Read(buffer)
		if n > 0 {
			if e := c.deps.stderr(c.deps.ctx, string(bytes.ToValidUTF8(buffer[:n], []byte("�")))); e != nil {
				c.deps.fail(e)
				return
			}
		}
		if err != nil {
			if err != io.EOF {
				c.deps.fail(err)
			}
			return
		}
	}
}
