package runner

import (
	"context"
	"errors"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/runner/control"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"github.com/coder/websocket"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	maxExecutorChannelsPerRunner = 8
	executorQueueDepth           = 4
	maxExecutorMessageBytes      = 1 << 20
	executorDialTimeout          = 20 * time.Second
)

// Each control connection has its own replay counter and joins its workers before reconnecting.
type executorConnection struct {
	runner   *Runner
	ctx      context.Context
	epoch    uint64
	lastOpen uint64
	send     func(*controlpb.RunnerFrame) error
	sendData func(*controlpb.RunnerFrame) error
	workers  sync.WaitGroup
}

func (r *Runner) openExecutorConnection(ctx context.Context, epoch uint64, send, sendData func(*controlpb.RunnerFrame) error) control.ExecutorConnection {
	return &executorConnection{runner: r, ctx: ctx, epoch: epoch, send: send, sendData: sendData}
}

func (c *executorConnection) Route(frame *executorpb.ExecutorFrame) error {
	return c.runner.routeExecutorFrame(c.ctx, frame, c.epoch, &c.lastOpen, c.send, c.sendData, &c.workers)
}

func (c *executorConnection) Close() {
	c.runner.cancelExecutorRelays(c.epoch)
	c.workers.Wait()
}

type executorRelay struct {
	id        string
	sessionID string
	epoch     uint64
	inbound   chan *executorpb.ExecutorFrame
	done      chan struct{}
	cancel    context.CancelFunc
	finish    sync.Once
	readyMu   sync.Mutex
	ready     bool
}

func newExecutorRelay(id, sessionID string, cancel context.CancelFunc) *executorRelay {
	return &executorRelay{
		id: id, sessionID: sessionID, inbound: make(chan *executorpb.ExecutorFrame, executorQueueDepth),
		done: make(chan struct{}), cancel: cancel,
	}
}

func (r *Runner) startExecutorRelay(
	streamCtx context.Context,
	channelID string,
	epoch uint64,
	open *executorpb.ExecutorOpen,
	send func(*controlpb.RunnerFrame) error,
	sendData func(*controlpb.RunnerFrame) error,
	workers *sync.WaitGroup,
) error {
	if r.codexExecServer == nil {
		return status.Error(codes.FailedPrecondition, "Runner has no configured Codex executor")
	}
	if channelID == "" || open.GetSessionId() == "" {
		return status.Error(codes.InvalidArgument, "executor channel ID and Session ID are required")
	}
	r.mu.Lock()
	if r.closing || !r.lifecycle.accepting() {
		r.mu.Unlock()
		return status.Error(codes.FailedPrecondition, "Runner is quiescing")
	}
	if len(r.executorRelays) >= maxExecutorChannelsPerRunner {
		r.mu.Unlock()
		return status.Error(codes.ResourceExhausted, "Runner has too many executor channels")
	}
	if _, exists := r.executorRelays[channelID]; exists {
		r.mu.Unlock()
		return status.Error(codes.AlreadyExists, "executor channel was already opened")
	}
	if err := r.codexExecServer.RequestLocked(r.ctx); err != nil {
		r.mu.Unlock()
		return status.Errorf(codes.FailedPrecondition, "request Codex executor: %v", err)
	}
	ctx, cancel := context.WithCancel(streamCtx)
	relay := newExecutorRelay(channelID, open.GetSessionId(), cancel)
	relay.epoch = epoch
	r.executorRelays[channelID] = relay
	r.mu.Unlock()
	r.changed()
	workers.Add(1)
	go func() {
		defer workers.Done()
		r.runExecutorRelay(ctx, relay, send, sendData)
	}()
	return nil
}

func (r *Runner) runExecutorRelay(
	ctx context.Context,
	relay *executorRelay,
	send func(*controlpb.RunnerFrame) error,
	sendData func(*controlpb.RunnerFrame) error,
) {
	defer func() {
		relay.cancel()
		r.mu.Lock()
		if r.executorRelays[relay.id] == relay {
			delete(r.executorRelays, relay.id)
		}
		r.mu.Unlock()
		r.changed()
		relay.finish.Do(func() { close(relay.done) })
	}()

	// Demand is admitted with the relay under Runner.mu. Only this channel's
	// worker waits for controller-owned readiness; the control stream stays free. The
	// Gateway owns the public readiness deadline, so do not cut off controller retries
	// with a second, shorter timer here.
	last, err := r.waitForExecutorReadiness(ctx)
	if err != nil {
		slog.Warn(
			"central executor channel could not start",
			"session_id", relay.sessionID, "error", err, "executor_runtime_found", last.runtimeFound,
			"executor_phase", last.runtime.Phase, "executor_process_id", last.runtime.ProcessID,
			"executor_generation", last.runtime.Generation, "executor_ready_deadline", last.runtime.ReadyDeadline,
			"executor_error", last.runtime.Error, "executor_refusal", last.runtime.Refusal,
			"executor_supervision_found", last.processFound, "executor_supervision_phase", last.process.Phase,
			"executor_supervision_started", last.process.Started, "executor_supervision_start_error", last.process.StartError,
			"executor_supervision_start_refusal", last.process.StartRefusal,
			"executor_supervision_exit_error", last.process.ExitError,
		)
		_ = sendExecutorReady(send, relay.id, false)
		return
	}
	dialCtx, cancelDial := context.WithTimeout(ctx, executorDialTimeout)
	conn, _, err := websocket.Dial(dialCtx, r.codexExecServer.URL(), nil)
	cancelDial()
	if err != nil {
		slog.Warn("central executor channel could not connect", "session_id", relay.sessionID, "error", err)
		_ = sendExecutorReady(send, relay.id, false)
		return
	}
	conn.SetReadLimit(maxExecutorMessageBytes)
	relay.readyMu.Lock()
	if ctx.Err() != nil {
		relay.readyMu.Unlock()
		_ = conn.Close(websocket.StatusGoingAway, "Executor channel closed before readiness")
		return
	}
	// After connection, Close can use the ordered inbound queue. Do not hold
	// readyMu while sending: frame routing must never wait on tunnel writes.
	relay.ready = true
	relay.readyMu.Unlock()
	if err := sendExecutorReady(send, relay.id, true); err != nil {
		_ = conn.Close(websocket.StatusGoingAway, "Runner control stream ended")
		return
	}
	slog.Debug("central executor channel opened", "session_id", relay.sessionID)

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	closeOnce := sync.Once{}
	sendClose := func(code uint32, reason string) {
		closeOnce.Do(func() {
			_ = send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Executor{Executor: &executorpb.ExecutorFrame{
				ChannelId: relay.id,
				Frame:     &executorpb.ExecutorFrame_Close{Close: &executorpb.ExecutorClose{Code: code, Reason: reason}},
			}}})
		})
	}
	defer sendClose(uint32(websocket.StatusGoingAway), "Runner executor channel closed")

	finished := make(chan error, 2)
	go func() {
		for {
			messageType, payload, err := conn.Read(ctx)
			if err != nil {
				if ctx.Err() == nil {
					code := websocket.CloseStatus(err)
					if !validExecutorCloseCode(uint32(code)) {
						code = websocket.StatusInternalError
					}
					sendClose(uint32(code), "")
				}
				finished <- err
				return
			}
			if len(payload) > maxExecutorMessageBytes {
				sendClose(uint32(websocket.StatusMessageTooBig), "executor message exceeds the size limit")
				finished <- status.Error(codes.ResourceExhausted, "executor message exceeds the size limit")
				return
			}
			opcode := executorpb.ExecutorMessage_OPCODE_TEXT
			if messageType == websocket.MessageBinary {
				opcode = executorpb.ExecutorMessage_OPCODE_BINARY
			}
			frame := &controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Executor{Executor: &executorpb.ExecutorFrame{
				ChannelId: relay.id,
				Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{
					Opcode: opcode, Payload: payload,
				}},
			}}}
			if err := sendData(frame); err != nil {
				finished <- err
				return
			}
		}
	}()
	go func() {
		for {
			select {
			case frame := <-relay.inbound:
				switch body := frame.GetFrame().(type) {
				case *executorpb.ExecutorFrame_Message:
					if len(body.Message.GetPayload()) > maxExecutorMessageBytes {
						_ = conn.Close(websocket.StatusMessageTooBig, "executor message exceeds the size limit")
						finished <- status.Error(codes.ResourceExhausted, "executor message exceeds the size limit")
						return
					}
					messageType := websocket.MessageText
					if body.Message.GetOpcode() == executorpb.ExecutorMessage_OPCODE_BINARY {
						messageType = websocket.MessageBinary
					} else if body.Message.GetOpcode() != executorpb.ExecutorMessage_OPCODE_TEXT {
						finished <- status.Error(codes.InvalidArgument, "unknown executor message opcode")
						return
					}
					if err := conn.Write(ctx, messageType, body.Message.GetPayload()); err != nil {
						finished <- err
						return
					}
				case *executorpb.ExecutorFrame_Close:
					code := normalizeExecutorCloseCode(body.Close.GetCode())
					_ = conn.Close(websocket.StatusCode(code), body.Close.GetReason())
					sendClose(code, body.Close.GetReason())
					finished <- nil
					return
				default:
					finished <- status.Error(codes.InvalidArgument, "invalid executor frame from Gateway")
					return
				}
			case <-relay.done:
				finished <- context.Canceled
				return
			case <-ctx.Done():
				finished <- ctx.Err()
				return
			}
		}
	}()

	first := <-finished
	cancel()
	_ = conn.Close(websocket.StatusNormalClosure, "executor channel closed")
	<-finished
	if first != nil && !errors.Is(first, context.Canceled) && ctx.Err() == nil {
		slog.Debug("central executor channel ended", "session_id", relay.sessionID, "error", first)
	}
}

type executorReadinessObservation struct {
	runtime      codexexecserver.Runtime
	runtimeFound bool
	process      supervision.Status
	processFound bool
}

func (r *Runner) waitForExecutorReadiness(ctx context.Context) (executorReadinessObservation, error) {
	var last executorReadinessObservation
	err := r.lifecycle.waitFor(ctx, func() (bool, error) {
		current, runtimeFound, err := r.codexExecServer.RuntimeState(ctx)
		if err != nil {
			return false, err
		}
		last.runtime, last.runtimeFound = current, runtimeFound
		if current.ProcessID != "" {
			process, found, err := supervision.ReadStatus(ctx, r.lifecycle.state, runnerlifecycle.Namespace, current.ProcessID)
			if err != nil {
				return false, err
			}
			last.process, last.processFound = process, found
		} else {
			last.process, last.processFound = supervision.Status{}, false
		}
		if current.Refusal != "" {
			return true, codexexecserver.Refusal{Reason: current.Refusal}
		}
		if r.lifecycle.desiredState().Mode != runnerlifecycle.Running {
			return true, errors.New("Codex executor is stopping")
		}
		if current.StopError != "" {
			return true, errors.New(current.StopError)
		}
		if current.Phase == codexexecserver.Ready {
			return true, nil
		}
		if current.Phase == codexexecserver.Stopped && current.Error != "" {
			return true, errors.New(current.Error)
		}
		return false, nil
	})
	return last, err
}

func sendExecutorReady(send func(*controlpb.RunnerFrame) error, id string, accepted bool) error {
	return send(&controlpb.RunnerFrame{Frame: &controlpb.RunnerFrame_Executor{Executor: &executorpb.ExecutorFrame{
		ChannelId: id,
		Frame:     &executorpb.ExecutorFrame_Ready{Ready: &executorpb.ExecutorReady{Accepted: accepted}},
	}}})
}

func normalizeExecutorCloseCode(code uint32) uint32 {
	if code == 0 {
		return uint32(websocket.StatusNormalClosure)
	}
	if !validExecutorCloseCode(code) {
		return uint32(websocket.StatusInternalError)
	}
	return code
}

func validExecutorCloseCode(code uint32) bool {
	if code >= 3000 && code <= 4999 {
		return true
	}
	switch code {
	case 1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014:
		return true
	default:
		return false
	}
}

func parseExecutorChannelID(value string, epoch, previous uint64) (uint64, error) {
	parts := strings.Split(value, "-")
	if len(parts) != 2 {
		return 0, status.Error(codes.InvalidArgument, "invalid executor channel ID")
	}
	frameEpoch, err := strconv.ParseUint(parts[0], 10, 64)
	if err != nil || frameEpoch != epoch {
		return 0, status.Error(codes.InvalidArgument, "executor channel belongs to another connection epoch")
	}
	sequence, err := strconv.ParseUint(parts[1], 10, 64)
	if err != nil || sequence == 0 || sequence <= previous {
		return 0, status.Error(codes.AlreadyExists, "executor channel ID was replayed")
	}
	return sequence, nil
}

func (r *Runner) routeExecutorFrame(ctx context.Context, frame *executorpb.ExecutorFrame, epoch uint64, lastOpen *uint64, send func(*controlpb.RunnerFrame) error, sendData func(*controlpb.RunnerFrame) error, workers *sync.WaitGroup) error {
	if frame.GetChannelId() == "" {
		return status.Error(codes.InvalidArgument, "Gateway executor frame has no channel ID")
	}
	switch body := frame.GetFrame().(type) {
	case *executorpb.ExecutorFrame_Open:
		sequence, err := parseExecutorChannelID(frame.GetChannelId(), epoch, *lastOpen)
		if err != nil {
			return err
		}
		*lastOpen = sequence
		if err := r.startExecutorRelay(ctx, frame.GetChannelId(), epoch, body.Open, send, sendData, workers); err != nil {
			slog.Debug("central executor channel refused", "session_id", body.Open.GetSessionId(), "error", err)
			return sendExecutorReady(send, frame.GetChannelId(), false)
		}
		return nil
	case *executorpb.ExecutorFrame_Message, *executorpb.ExecutorFrame_Close:
		r.mu.Lock()
		relay := r.executorRelays[frame.GetChannelId()]
		r.mu.Unlock()
		if relay == nil {
			return nil // delayed data after a channel close cannot be replayed onto a later channel
		}
		if frame.GetClose() != nil {
			// A Gateway timeout can close the channel while this worker waits for
			// controller readiness. Cancel that wait immediately instead of leaving
			// a stale relay until a future executor retry succeeds.
			relay.readyMu.Lock()
			if !relay.ready {
				relay.cancel()
			}
			relay.readyMu.Unlock()
		}
		if message := frame.GetMessage(); message != nil && len(message.GetPayload()) > maxExecutorMessageBytes {
			return status.Error(codes.ResourceExhausted, "executor message exceeds the size limit")
		}
		select {
		case relay.inbound <- frame:
			return nil
		default:
			relay.cancel()
			return status.Error(codes.ResourceExhausted, "executor channel queue is full")
		}
	default:
		return status.Error(codes.InvalidArgument, "invalid executor frame from Gateway")
	}
}

func (r *Runner) closeExecutorRelays(ctx context.Context) error {
	r.mu.Lock()
	relays := make([]*executorRelay, 0, len(r.executorRelays))
	for _, relay := range r.executorRelays {
		relays = append(relays, relay)
		relay.cancel()
	}
	r.mu.Unlock()
	for _, relay := range relays {
		select {
		case <-relay.done:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func (r *Runner) cancelExecutorRelays(epoch uint64) {
	r.mu.Lock()
	for _, relay := range r.executorRelays {
		if relay.epoch == epoch {
			relay.cancel()
		}
	}
	r.mu.Unlock()
}
