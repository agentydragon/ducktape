package orchestrationv1

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/session_client"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// A token is a cursor, not authority: every call checks the live Sandbox and Session again.
type turnResume struct {
	Version    int    `json:"v"`
	SandboxUID string `json:"sandbox_uid"`
	SessionID  string `json:"session_id"`
	Cursor     uint64 `json:"cursor"`
}

func resumeToken(ref contract.SandboxRef, sessionID string, cursor uint64) string {
	encoded, err := json.Marshal(turnResume{1, string(ref.UID), sessionID, cursor})
	if err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(encoded)
}

func resumeCursor(token string, ref contract.SandboxRef, sessionID string) (uint64, error) {
	encoded, err := base64.RawURLEncoding.DecodeString(token)
	var resume turnResume
	if err != nil || json.Unmarshal(encoded, &resume) != nil || resume.Version != 1 || resume.SandboxUID != string(ref.UID) || resume.SessionID != sessionID {
		return 0, refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INVALID_ARGUMENTS, "invalid resume token for this Session and Sandbox incarnation")
	}
	return resume.Cursor, nil
}

type Turns struct {
	Turns       []sessionclient.TurnView `json:"turns"`
	ResumeToken string                   `json:"resume_token"`
}

type TurnCompletion struct {
	TurnID string  `json:"turn_id"`
	Status string  `json:"status"`
	Error  *string `json:"error,omitempty"`
}

type TurnWait struct {
	Completed   bool            `json:"completed"`
	Turn        *TurnCompletion `json:"turn,omitempty"`
	ResumeToken string          `json:"resume_token"`
}

func (e *Executor) sessionReadContext(ctx context.Context) (context.Context, context.CancelFunc, error) {
	if e.readBudget <= 0 {
		return nil, nil, refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_WAIT_UNAVAILABLE, "direct-tool budget must exceed one second for Session reads")
	}
	bounded, cancel := context.WithTimeout(ctx, e.readBudget)
	return bounded, cancel, nil
}

func (e *Executor) readTurns(ctx context.Context, args contract.ReadTurnsArgs) (Turns, error) {
	ctx, cancel, err := e.sessionReadContext(ctx)
	if err != nil {
		return Turns{}, err
	}
	defer cancel()
	if _, _, _, err := e.sessionForRead(ctx, args.Sandbox, args.SessionID); err != nil {
		return Turns{}, err
	}
	stream, err := e.history.FollowSession(ctx, &historypb.FollowSessionRequest{
		SessionId: args.SessionID,
		Start:     &historypb.FollowSessionRequest_Back{Back: &historypb.StartBack{Kind: historypb.EventKind_EVENT_KIND_TURN_STARTED, Count: uint32(args.Count)}},
		Kinds:     []historypb.EventKind{historypb.EventKind_EVENT_KIND_HARNESS_USER_MESSAGE_CONFIRMED, historypb.EventKind_EVENT_KIND_TURN_STARTED, historypb.EventKind_EVENT_KIND_TURN_COMPLETED, historypb.EventKind_EVENT_KIND_ITEM_STARTED, historypb.EventKind_EVENT_KIND_ITEM_COMPLETED},
	})
	if err != nil {
		return Turns{}, readError("History Service", err)
	}
	result := Turns{Turns: []sessionclient.TurnView{}}
	projection := sessionclient.TurnProjection{}
	for {
		frame, err := stream.Recv()
		if err != nil {
			return Turns{}, readError("History Service", err)
		}
		for _, entry := range frame.GetEntries().GetEntries() {
			completed, err := projection.Add(entry)
			if err != nil {
				return Turns{}, readError("History Service", status.Error(codes.DataLoss, "retained History contains an incomplete turn projection"))
			}
			if completed != nil {
				result.Turns = append(result.Turns, *completed)
			}
		}
		switch frame.GetFrame().(type) {
		case *historypb.FollowSessionResponse_CaughtUp:
			if partial := projection.Partial(); partial != nil {
				result.Turns = append(result.Turns, *partial)
			}
			result.ResumeToken = resumeToken(args.Sandbox, args.SessionID, frame.GetThroughCursor())
			return result, nil
		case *historypb.FollowSessionResponse_ReconnectRequired:
			// A bounded read must finish in this Action, rather than returning a partial turn listing.
			return Turns{}, readError("History Service", status.Error(codes.Unavailable, "History read requires reconnect"))
		case *historypb.FollowSessionResponse_Removed:
			return Turns{}, readError("History Service", status.Error(codes.DataLoss, "turn history contains a removed range"))
		}
	}
}

func (e *Executor) waitForTurn(parent context.Context, args contract.WaitForTurnArgs) (TurnWait, error) {
	cursor, err := resumeCursor(args.ResumeToken, args.Sandbox, args.SessionID)
	if err != nil {
		return TurnWait{}, err
	}
	readCtx, cancelRead, err := e.sessionReadContext(parent)
	if err != nil {
		return TurnWait{}, err
	}
	defer cancelRead()
	if _, _, _, err := e.sessionForRead(readCtx, args.Sandbox, args.SessionID); err != nil {
		return TurnWait{}, err
	}
	ctx, cancel := context.WithTimeout(readCtx, time.Duration(args.WaitSeconds*float64(time.Second)))
	defer cancel()
	deadline, _ := ctx.Deadline()
	result := TurnWait{ResumeToken: resumeToken(args.Sandbox, args.SessionID, cursor)}
	for {
		stream, err := e.history.FollowSession(ctx, &historypb.FollowSessionRequest{SessionId: args.SessionID, Start: &historypb.FollowSessionRequest_AfterCursor{AfterCursor: cursor}, Kinds: []historypb.EventKind{historypb.EventKind_EVENT_KIND_TURN_COMPLETED}, Follow: true})
		if err == nil {
			for {
				var frame *historypb.FollowSessionResponse
				frame, err = stream.Recv()
				if err != nil {
					break
				}
				for _, entry := range frame.GetEntries().GetEntries() {
					if completed := entry.GetEvent().GetTurnCompleted(); completed != nil {
						result.Completed = true
						result.Turn = &TurnCompletion{TurnID: completed.GetTurnId(), Status: completed.GetStatus().String()}
						if completed.GetError() != "" {
							result.Turn.Error = new(completed.GetError())
						}
						result.ResumeToken = resumeToken(args.Sandbox, args.SessionID, entry.GetCursor())
						return result, nil
					}
				}
				cursor = frame.GetThroughCursor()
				result.ResumeToken = resumeToken(args.Sandbox, args.SessionID, cursor)
				if _, renew := frame.GetFrame().(*historypb.FollowSessionResponse_ReconnectRequired); renew {
					break
				}
			}
		}
		// History can exhaust the propagated deadline before the local context timer fires.
		if ctx.Err() != nil || (status.Code(err) == codes.DeadlineExceeded && !time.Now().Before(deadline)) {
			if parent.Err() == nil {
				return result, nil
			}
			if err == nil {
				err = ctx.Err()
			}
		}
		if err == nil {
			continue
		}
		if err == io.EOF {
			err = status.Error(codes.Unavailable, "History follow ended without reconnect")
		}
		return TurnWait{}, readError("History Service", fmt.Errorf("follow Session: %w", err))
	}
}
