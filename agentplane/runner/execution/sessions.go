package execution

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	executionpb "github.com/agentydragon/ducktape/agentplane/proto/executionpb"
	runnerexecpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	"github.com/coder/websocket"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const openSessionSendTimeout = 10 * time.Second

func sendOpenSessionResponse(ctx context.Context, stream runnerexecpb.RunnerExec_OpenSessionServer, response *executionpb.OpenSessionResponse) error {
	return streaming.SendWithin(ctx, openSessionSendTimeout, stream.Send, response)
}

// OpenSession holds one local WebSocket for the stream lifetime. Closing the
// stream closes the connection; it never sends process/terminate.
func (a *Adapter) OpenSession(r *executionpb.OpenSessionRequest, stream runnerexecpb.RunnerExec_OpenSessionServer) error {
	if r.RequestId == "" || len(r.RequestId) > 256 {
		return invalid("request ID is required and limited to 256 bytes")
	}
	if r.ResumeSessionId != nil && r.GetResumeSessionId() == "" {
		return invalid("resume execution Session ID cannot be empty")
	}
	var sessionCtx context.Context
	var sessionCancel context.CancelFunc
	var slotID uint64
	var slotDone chan struct{}
	var previousCancel context.CancelFunc
	var previousDone chan struct{}
	opening := true
	err := a.authorize(stream.Context(), r.Scope, true, func(admittedCtx context.Context) error {
		a.mu.Lock()
		defer a.mu.Unlock()
		if a.closed || a.draining || a.ctx.Err() != nil {
			return status.Error(codes.Unavailable, "execution adapter is stopping")
		}
		if r.ResumeSessionId != nil {
			if previous := a.sessions[*r.ResumeSessionId]; previous != nil {
				if !proto.Equal(previous.scope, r.Scope) {
					return status.Error(codes.PermissionDenied, "execution session belongs to a different scope")
				}
				previousCancel = a.slots[previous.slotID]
				previousDone = a.slotDone[previous.slotID]
				delete(a.sessions, *r.ResumeSessionId)
			}
		}
		if a.opening+len(a.sessions) >= maxSessions {
			return status.Error(codes.ResourceExhausted, "Runner has too many open execution sessions")
		}
		ctx, cancel := context.WithCancel(admittedCtx)
		stopRoot := context.AfterFunc(a.ctx, cancel)
		sessionCtx = ctx
		sessionCancel = func() {
			stopRoot()
			cancel()
		}
		a.nextSlot++
		slotID = a.nextSlot
		slotDone = make(chan struct{})
		a.slots[slotID] = sessionCancel
		a.slotDone[slotID] = slotDone
		a.opening++
		a.workers.Add(1)
		return nil
	})
	if err != nil {
		return err
	}
	defer a.workers.Done()
	stopCaller := context.AfterFunc(stream.Context(), sessionCancel)
	defer stopCaller()
	var c *connection
	registered := false
	defer func() {
		a.mu.Lock()
		done := a.slotDone[slotID]
		if opening {
			a.opening--
		}
		if c != nil && a.sessions[c.id] == c {
			delete(a.sessions, c.id)
		}
		delete(a.slots, slotID)
		delete(a.slotDone, slotID)
		if done != nil {
			close(done)
		}
		a.mu.Unlock()
		sessionCancel()
		a.changed()
	}()
	if previousCancel != nil {
		previousCancel()
		select {
		case <-previousDone:
		case <-sessionCtx.Done():
			return status.FromContextError(sessionCtx.Err()).Err()
		}
	}
	a.changed()
	response := new(executionpb.OpenSessionResponse)
	var frames []*executionpb.ExecutionNativeFrame
	endpoint, err := a.config.Acquire(sessionCtx)
	if err == nil {
		dialCtx, cancelDial := context.WithTimeout(sessionCtx, 20*time.Second)
		var ws *websocket.Conn
		ws, _, err = websocket.Dial(dialCtx, endpoint, nil)
		cancelDial()
		if err == nil {
			ws.SetReadLimit(maxNativeBytes)
			wsCtx, wsCancel := context.WithCancel(sessionCtx)
			current := &connection{
				scope: proto.Clone(r.Scope).(*executionpb.ExecutionScope), id: "", ws: ws,
				slotID: slotID,
				ctx:    wsCtx, cancel: wsCancel,
				notifications: make(chan []byte, maxQueuedNotifications), calls: make(chan struct{}, 64),
			}
			readDone := make(chan struct{})
			current.done = readDone
			stopSession := context.AfterFunc(sessionCtx, current.stop)
			defer stopSession()
			go func() { current.read(); close(readDone) }()
			defer func() { current.stop(); <-readDone }()
			var initialized *executorInitialized
			initialized, err = rpcResult[executorInitialized](current, r.RequestId, "initialize", executorInitialize{ClientName: "agentplane-runner", ResumeSessionID: r.ResumeSessionId}, &frames)
			if err != nil {
				if _, raw := current.failure(); len(raw) != 0 {
					frames = append(frames, &executionpb.ExecutionNativeFrame{Direction: "response", Payload: raw})
				}
			}
			if err == nil && (initialized.SessionID == "" || r.ResumeSessionId != nil && initialized.SessionID != *r.ResumeSessionId) {
				err = errors.New("executor initialized an unexpected execution session")
			}
			if err == nil {
				err = current.initialized(&frames)
			}
			if err == nil {
				current.id = initialized.SessionID
				a.mu.Lock()
				if a.closed || a.draining || sessionCtx.Err() != nil {
					err = errors.New("execution adapter stopped during initialization")
				} else if a.sessions[current.id] != nil {
					err = &executorRejectedError{method: "initialize", code: -32602, message: "execution session already has an open connection"}
				} else {
					a.opening--
					opening = false
					a.sessions[current.id] = current
					c = current
					registered = true
				}
				a.mu.Unlock()
				if err == nil {
					response.ExecutionSessionId = current.id
					if initialized.EnvironmentInfo != nil {
						response.Shell = &executionpb.ExecutionShell{Name: initialized.EnvironmentInfo.Shell.Name, Path: initialized.EnvironmentInfo.Shell.Path}
					}
				}
			}
		}
	}
	if status.Code(err) == codes.FailedPrecondition {
		err = &executionAdmissionRejectedError{message: status.Convert(err).Message()}
	}
	response.Status = executionOutcome(err)
	response.NativeFrames = frames
	if err := sendOpenSessionResponse(sessionCtx, stream, response); err != nil {
		return err
	}
	if !registered {
		return nil
	}
	a.changed()
	for {
		select {
		case payload := <-c.notifications:
			if err := sendOpenSessionResponse(sessionCtx, stream, &executionpb.OpenSessionResponse{
				ExecutionSessionId: c.id,
				NativeFrames:       []*executionpb.ExecutionNativeFrame{{Direction: "notification", Payload: payload}},
			}); err != nil {
				return err
			}
		case <-sessionCtx.Done():
			if err := stream.Context().Err(); err != nil {
				return status.FromContextError(err).Err()
			}
			return status.FromContextError(sessionCtx.Err()).Err()
		case <-c.done:
			if sessionCtx.Err() != nil {
				if err := stream.Context().Err(); err != nil {
					return status.FromContextError(err).Err()
				}
				return status.FromContextError(sessionCtx.Err()).Err()
			}
			for {
				select {
				case payload := <-c.notifications:
					if err := sendOpenSessionResponse(sessionCtx, stream, &executionpb.OpenSessionResponse{
						ExecutionSessionId: c.id,
						NativeFrames:       []*executionpb.ExecutionNativeFrame{{Direction: "notification", Payload: payload}},
					}); err != nil {
						return err
					}
				default:
					goto notificationsDrained
				}
			}
		notificationsDrained:
			cause, raw := c.failure()
			if len(raw) != 0 {
				if err := sendOpenSessionResponse(sessionCtx, stream, &executionpb.OpenSessionResponse{
					ExecutionSessionId: c.id,
					NativeFrames:       []*executionpb.ExecutionNativeFrame{{Direction: "notification", Payload: raw}},
				}); err != nil {
					return err
				}
			}
			if cause == nil {
				cause = sessionCtx.Err()
			}
			return status.Errorf(codes.Unavailable, "local execution session connection ended: %v", cause)
		}
	}
}
