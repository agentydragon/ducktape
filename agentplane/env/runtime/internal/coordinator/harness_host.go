package coordinator

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// harnessHost translates the new RPC surface to Runtime's shared admission behavior.
type harnessHost struct {
	harnesshostpb.UnimplementedHarnessHostServer
	runtime *Runtime
}

func (h *harnessHost) CreateSession(ctx context.Context, request *harnesshostpb.CreateSessionRequest) (*harnesshostpb.CreateSessionResponse, error) {
	attached, err := h.runtime.createSession(ctx, request)
	return &harnesshostpb.CreateSessionResponse{Attached: attached}, err
}

func (h *harnessHost) SubmitInput(ctx context.Context, request *harnesshostpb.SubmitInputRequest) (*harnesshostpb.SubmitInputResponse, error) {
	admission, err := h.runtime.submitInput(ctx, request.GetSessionId(), request.GetCommandId(), request.GetText())
	return &harnesshostpb.SubmitInputResponse{Admission: admission}, err
}

func (h *harnessHost) ChangeModel(ctx context.Context, request *harnesshostpb.ChangeModelRequest) (*harnesshostpb.ChangeModelResponse, error) {
	admission, err := h.runtime.changeModel(ctx, request.GetSessionId(), request.GetCommandId(), request.GetModel())
	return &harnesshostpb.ChangeModelResponse{Admission: admission}, err
}

func (h *harnessHost) ChangeReasoningEffort(ctx context.Context, request *harnesshostpb.ChangeReasoningEffortRequest) (*harnesshostpb.ChangeReasoningEffortResponse, error) {
	admission, err := h.runtime.changeReasoningEffort(ctx, request.GetSessionId(), request.GetCommandId(), request.GetEffort())
	return &harnesshostpb.ChangeReasoningEffortResponse{Admission: admission}, err
}

func (h *harnessHost) InterruptTurn(ctx context.Context, request *harnesshostpb.InterruptTurnRequest) (*harnesshostpb.InterruptTurnResponse, error) {
	admission, err := h.runtime.interruptTurn(ctx, request.GetSessionId(), request.GetCommandId(), request.GetTurnId())
	return &harnesshostpb.InterruptTurnResponse{Admission: admission}, err
}

func (h *harnessHost) ListSessions(ctx context.Context, _ *harnesshostpb.ListSessionsRequest) (*harnesshostpb.ListSessionsResponse, error) {
	sessions, err := h.runtime.harnessHostSessions(ctx)
	if err != nil {
		return nil, err
	}
	if err := grpc.SetHeader(ctx, metadata.Pairs(harnesshost.SessionVersionMetadataKey, harnesshost.SessionVersion)); err != nil {
		return nil, err
	}
	return &harnesshostpb.ListSessionsResponse{Sessions: sessions}, err
}

func (h *harnessHost) WatchSessions(_ *harnesshostpb.WatchSessionsRequest, stream grpc.ServerStreamingServer[harnesshostpb.WatchSessionsResponse]) error {
	if err := stream.SendHeader(metadata.Pairs(harnesshost.SessionVersionMetadataKey, harnesshost.SessionVersion)); err != nil {
		return err
	}
	return h.runtime.watchSessions(stream.Context(), stream.Send)
}
