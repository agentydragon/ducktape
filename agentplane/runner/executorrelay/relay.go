package executorrelay

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	"github.com/coder/websocket"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	maxExecutorStreamsPerRunner = 8
	executorQueueDepth          = 4
	maxExecutorMessageBytes     = 1 << 20
	executorDialTimeout         = 20 * time.Second
)

type relay struct {
	id        string
	sessionID string
	inbound   chan *executorpb.ExecutorFrame
	done      chan struct{}
	cancel    context.CancelFunc
	finish    sync.Once
	readyMu   sync.Mutex
	ready     bool
}

func newExecutorRelay(id, sessionID string, cancel context.CancelFunc) *relay {
	return &relay{
		id: id, sessionID: sessionID, inbound: make(chan *executorpb.ExecutorFrame, executorQueueDepth),
		done: make(chan struct{}), cancel: cancel,
	}
}

func (m *Manager) startRelay(
	streamCtx context.Context,
	relayID string,
	open *executorpb.ExecutorOpen,
	send func(*executorpb.ExecutorFrame) error,
	sendData func(*executorpb.ExecutorFrame) error,
	workers *sync.WaitGroup,
) error {
	if m.config.Executor == nil {
		return status.Error(codes.FailedPrecondition, "Runner has no configured Codex executor")
	}
	if relayID == "" || open.GetSessionId() == "" {
		return status.Error(codes.InvalidArgument, "executor relay ID and Session ID are required")
	}
	var channel *relay
	var ctx context.Context
	err := m.config.Admit(func() error {
		m.mu.Lock()
		defer m.mu.Unlock()
		if len(m.relays) >= maxExecutorStreamsPerRunner {
			return status.Error(codes.ResourceExhausted, "Runner has too many executor channels")
		}
		if _, exists := m.relays[relayID]; exists {
			return status.Error(codes.AlreadyExists, "executor channel was already opened")
		}
		if err := m.config.Executor.RequestLocked(m.config.Context); err != nil {
			return status.Errorf(codes.FailedPrecondition, "request Codex executor: %v", err)
		}
		var cancel context.CancelFunc
		ctx, cancel = context.WithCancel(streamCtx)
		channel = newExecutorRelay(relayID, open.GetSessionId(), cancel)
		m.relays[relayID] = channel
		return nil
	})
	if err != nil {
		return err
	}
	m.config.OnChange()
	workers.Add(1)
	go func() {
		defer workers.Done()
		m.runRelay(ctx, channel, send, sendData)
	}()
	return nil
}

func (m *Manager) runRelay(
	ctx context.Context,
	relay *relay,
	send func(*executorpb.ExecutorFrame) error,
	sendData func(*executorpb.ExecutorFrame) error,
) {
	defer func() {
		relay.cancel()
		m.mu.Lock()
		if m.relays[relay.id] == relay {
			delete(m.relays, relay.id)
		}
		m.mu.Unlock()
		m.config.OnChange()
		relay.finish.Do(func() { close(relay.done) })
	}()

	// Demand is admitted with the relay under the Runner admission lock. Only this channel's
	// worker waits for controller-owned readiness; the control stream stays free. The
	// Gateway owns the public readiness deadline, so do not cut off controller retries
	// with a second, shorter timer here.
	last, err := m.WaitReady(ctx)
	if err != nil {
		slog.Warn(
			"central executor channel could not start",
			"session_id", relay.sessionID, "error", err, "executor_runtime_found", last.RuntimeFound,
			"executor_phase", last.Runtime.Phase, "executor_process_id", last.Runtime.ProcessID,
			"executor_generation", last.Runtime.Generation, "executor_ready_deadline", last.Runtime.ReadyDeadline,
			"executor_error", last.Runtime.Error, "executor_refusal", last.Runtime.Refusal,
			"executor_supervision_found", last.ProcessFound, "executor_supervision_phase", last.Process.Phase,
			"executor_supervision_started", last.Process.Started, "executor_supervision_start_error", last.Process.StartError,
			"executor_supervision_start_refusal", last.Process.StartRefusal,
			"executor_supervision_exit_error", last.Process.ExitError,
		)
		_ = sendExecutorReady(send, false)
		return
	}
	dialCtx, cancelDial := context.WithTimeout(ctx, executorDialTimeout)
	conn, _, err := websocket.Dial(dialCtx, m.config.Executor.URL(), nil)
	cancelDial()
	if err != nil {
		slog.Warn("central executor channel could not connect", "session_id", relay.sessionID, "error", err)
		_ = sendExecutorReady(send, false)
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
	// readyMu while sending: frame routing must never wait on control stream writes.
	relay.ready = true
	relay.readyMu.Unlock()
	if err := sendExecutorReady(send, true); err != nil {
		_ = conn.Close(websocket.StatusGoingAway, "Runner control stream ended")
		return
	}
	slog.Debug("central executor channel opened", "session_id", relay.sessionID)

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	closeOnce := sync.Once{}
	sendClose := func(code uint32, reason string) {
		closeOnce.Do(func() {
			_ = send(&executorpb.ExecutorFrame{
				Frame: &executorpb.ExecutorFrame_Close{Close: &executorpb.ExecutorClose{Code: code, Reason: reason}},
			})
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
			frame := &executorpb.ExecutorFrame{
				Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{
					Opcode: opcode, Payload: payload,
				}},
			}
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
	streamActive := ctx.Err() == nil
	cancel()
	_ = conn.Close(websocket.StatusNormalClosure, "executor channel closed")
	<-finished
	if first != nil && !errors.Is(first, context.Canceled) && streamActive &&
		websocket.CloseStatus(first) != websocket.StatusNormalClosure && websocket.CloseStatus(first) != websocket.StatusGoingAway {
		slog.Warn("central executor channel ended", "session_id", relay.sessionID, "error", first)
	}
}

type Observation struct {
	Runtime      codexexecserver.Runtime
	RuntimeFound bool
	Process      supervision.Status
	ProcessFound bool
}

func (m *Manager) WaitReady(ctx context.Context) (Observation, error) {
	var last Observation
	err := m.config.WaitFor(ctx, func() (bool, error) {
		current, found, err := m.config.Executor.RuntimeState(ctx)
		if err != nil {
			return false, err
		}
		last.Runtime, last.RuntimeFound = current, found
		if current.ProcessID != "" {
			process, found, err := supervision.ReadStatus(ctx, m.config.State, runnerlifecycle.Namespace, current.ProcessID)
			if err != nil {
				return false, err
			}
			last.Process, last.ProcessFound = process, found
		} else {
			last.Process, last.ProcessFound = supervision.Status{}, false
		}
		if current.Refusal != "" {
			return true, codexexecserver.Refusal{Reason: current.Refusal}
		}
		if !m.config.IsRunning() {
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

func sendExecutorReady(send func(*executorpb.ExecutorFrame) error, accepted bool) error {
	return send(&executorpb.ExecutorFrame{
		Frame: &executorpb.ExecutorFrame_Ready{Ready: &executorpb.ExecutorReady{Accepted: accepted}},
	})
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

func (m *Manager) routeFrame(ctx context.Context, frame *executorpb.ExecutorFrame, relayID string, opened *bool, send func(*executorpb.ExecutorFrame) error, sendData func(*executorpb.ExecutorFrame) error, workers *sync.WaitGroup) error {
	if frame == nil || frame.GetFrame() == nil {
		return status.Error(codes.InvalidArgument, "executor frame is empty")
	}
	switch body := frame.GetFrame().(type) {
	case *executorpb.ExecutorFrame_Open:
		if *opened {
			return status.Error(codes.InvalidArgument, "executor stream already opened")
		}
		*opened = true
		if err := m.startRelay(ctx, relayID, body.Open, send, sendData, workers); err != nil {
			slog.Debug("central executor channel refused", "session_id", body.Open.GetSessionId(), "error", err)
			return sendExecutorReady(send, false)
		}
		return nil
	case *executorpb.ExecutorFrame_Message, *executorpb.ExecutorFrame_Close:
		if !*opened {
			return status.Error(codes.InvalidArgument, "executor stream must start with ExecutorOpen")
		}
		m.mu.Lock()
		relay := m.relays[relayID]
		m.mu.Unlock()
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
