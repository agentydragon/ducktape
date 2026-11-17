package coordinator

import (
	"context"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func (r *Runtime) CreateSession(ctx context.Context, request *environmentruntimepb.CreateSessionRequest) (*environmentruntimepb.CreateSessionResponse, error) {
	attached, err := r.createSession(ctx, &harnesshostpb.CreateSessionRequest{SessionId: request.GetSessionId(), Spec: request.GetSpec(), SetupScript: request.SetupScript, ExecutionIdentity: request.GetExecutionIdentity()})
	return &environmentruntimepb.CreateSessionResponse{Attached: attached}, err
}

func (r *Runtime) SubmitInput(ctx context.Context, request *environmentruntimepb.SubmitInputRequest) (*environmentruntimepb.SubmitInputResponse, error) {
	admission, err := r.submitInput(ctx, request.GetSessionId(), request.GetCommandId(), request.GetText())
	return &environmentruntimepb.SubmitInputResponse{Admission: admission}, err
}

func (r *Runtime) ChangeModel(ctx context.Context, request *environmentruntimepb.ChangeModelRequest) (*environmentruntimepb.ChangeModelResponse, error) {
	admission, err := r.changeModel(ctx, request.GetSessionId(), request.GetCommandId(), request.GetModel())
	return &environmentruntimepb.ChangeModelResponse{Admission: admission}, err
}

func (r *Runtime) ChangeReasoningEffort(ctx context.Context, request *environmentruntimepb.ChangeReasoningEffortRequest) (*environmentruntimepb.ChangeReasoningEffortResponse, error) {
	admission, err := r.changeReasoningEffort(ctx, request.GetSessionId(), request.GetCommandId(), request.GetEffort())
	return &environmentruntimepb.ChangeReasoningEffortResponse{Admission: admission}, err
}

func (r *Runtime) InterruptTurn(ctx context.Context, request *environmentruntimepb.InterruptTurnRequest) (*environmentruntimepb.InterruptTurnResponse, error) {
	admission, err := r.interruptTurn(ctx, request.GetSessionId(), request.GetCommandId(), request.GetTurnId())
	return &environmentruntimepb.InterruptTurnResponse{Admission: admission}, err
}

func (r *Runtime) ListSessions(ctx context.Context, _ *sessionpb.ListSessionsRequest) (*sessionpb.ListSessionsResponse, error) {
	sessions, err := r.listSessions(ctx)
	return &sessionpb.ListSessionsResponse{Sessions: sessions}, err
}
