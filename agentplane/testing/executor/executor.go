// Package executor provides a fake generated executor client for tests.
package executor

import (
	"context"
	"io"
	"sync"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"google.golang.org/grpc"
)

type Client struct {
	OnOpenSession      func(context.Context, *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error)
	OnStartProcess     func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error)
	OnReadProcess      func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error)
	OnWriteProcess     func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error)
	OnSignalProcess    func(context.Context, *executorpb.SignalProcessRequest) (*executorpb.SignalProcessResponse, error)
	OnTerminateProcess func(context.Context, *executorpb.TerminateProcessRequest) (*executorpb.TerminateProcessResponse, error)
	OnGetFileMetadata  func(context.Context, *executorpb.GetFileMetadataRequest) (*executorpb.GetFileMetadataResponse, error)
	OnOpenFile         func(context.Context, *executorpb.OpenFileRequest) (*executorpb.OpenFileResponse, error)
	OnReadFile         func(context.Context, *executorpb.ReadFileRequest) (*executorpb.ReadFileResponse, error)
	OnCloseFile        func(context.Context, *executorpb.CloseFileRequest) (*executorpb.CloseFileResponse, error)
}

func (c *Client) OpenSession(ctx context.Context, request *executorpb.OpenSessionRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
	if c.OnOpenSession != nil {
		return c.OnOpenSession(ctx, request)
	}
	sessionID := request.GetResumeSessionId()
	if sessionID == "" {
		sessionID = "fake-execution-session"
	}
	return &Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{
		Status: CompletedStatus(), ExecutionSessionId: sessionID,
	}}, nil
}

func (c *Client) StartProcess(ctx context.Context, request *executorpb.StartProcessRequest, _ ...grpc.CallOption) (*executorpb.StartProcessResponse, error) {
	if c.OnStartProcess != nil {
		return c.OnStartProcess(ctx, request)
	}
	return &executorpb.StartProcessResponse{Status: CompletedStatus(), ProcessId: request.GetProcessId()}, nil
}

func (c *Client) ReadProcess(ctx context.Context, request *executorpb.ReadProcessRequest, _ ...grpc.CallOption) (*executorpb.ReadProcessResponse, error) {
	if c.OnReadProcess != nil {
		return c.OnReadProcess(ctx, request)
	}
	return &executorpb.ReadProcessResponse{Status: CompletedStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}}, nil
}

func (c *Client) WriteProcess(ctx context.Context, request *executorpb.WriteProcessRequest, _ ...grpc.CallOption) (*executorpb.WriteProcessResponse, error) {
	if c.OnWriteProcess != nil {
		return c.OnWriteProcess(ctx, request)
	}
	return &executorpb.WriteProcessResponse{Status: CompletedStatus(), WriteStatus: "accepted"}, nil
}

func (c *Client) SignalProcess(ctx context.Context, request *executorpb.SignalProcessRequest, _ ...grpc.CallOption) (*executorpb.SignalProcessResponse, error) {
	if c.OnSignalProcess != nil {
		return c.OnSignalProcess(ctx, request)
	}
	return &executorpb.SignalProcessResponse{Status: CompletedStatus()}, nil
}

func (c *Client) TerminateProcess(ctx context.Context, request *executorpb.TerminateProcessRequest, _ ...grpc.CallOption) (*executorpb.TerminateProcessResponse, error) {
	if c.OnTerminateProcess != nil {
		return c.OnTerminateProcess(ctx, request)
	}
	return &executorpb.TerminateProcessResponse{Status: CompletedStatus()}, nil
}

func (c *Client) GetFileMetadata(ctx context.Context, request *executorpb.GetFileMetadataRequest, _ ...grpc.CallOption) (*executorpb.GetFileMetadataResponse, error) {
	if c.OnGetFileMetadata != nil {
		return c.OnGetFileMetadata(ctx, request)
	}
	return &executorpb.GetFileMetadataResponse{Status: CompletedStatus()}, nil
}

func (c *Client) OpenFile(ctx context.Context, request *executorpb.OpenFileRequest, _ ...grpc.CallOption) (*executorpb.OpenFileResponse, error) {
	if c.OnOpenFile != nil {
		return c.OnOpenFile(ctx, request)
	}
	return &executorpb.OpenFileResponse{Status: CompletedStatus(), HandleId: request.GetHandleId()}, nil
}

func (c *Client) ReadFile(ctx context.Context, request *executorpb.ReadFileRequest, _ ...grpc.CallOption) (*executorpb.ReadFileResponse, error) {
	if c.OnReadFile != nil {
		return c.OnReadFile(ctx, request)
	}
	return &executorpb.ReadFileResponse{Status: CompletedStatus(), Eof: true}, nil
}

func (c *Client) CloseFile(ctx context.Context, request *executorpb.CloseFileRequest, _ ...grpc.CallOption) (*executorpb.CloseFileResponse, error) {
	if c.OnCloseFile != nil {
		return c.OnCloseFile(ctx, request)
	}
	return &executorpb.CloseFileResponse{Status: CompletedStatus()}, nil
}

func CompletedStatus() *executorpb.ExecutionStatus {
	return &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED}
}

// Stream returns one configured response, then ends with EOF when its context is canceled.
type Stream struct {
	grpc.ClientStream
	Ctx     context.Context
	Initial *executorpb.OpenSessionResponse
	once    sync.Once
}

func (s *Stream) Recv() (*executorpb.OpenSessionResponse, error) {
	var response *executorpb.OpenSessionResponse
	s.once.Do(func() { response = s.Initial })
	if response != nil {
		return response, nil
	}
	<-s.Ctx.Done()
	return nil, io.EOF
}

type FailedStream struct {
	grpc.ClientStream
	Err error
}

func (s FailedStream) Recv() (*executorpb.OpenSessionResponse, error) { return nil, s.Err }

type NotificationStream struct {
	grpc.ClientStream
	Ctx       context.Context
	Responses chan *executorpb.OpenSessionResponse
}

func (s *NotificationStream) Recv() (*executorpb.OpenSessionResponse, error) {
	select {
	case response := <-s.Responses:
		return response, nil
	case <-s.Ctx.Done():
		return nil, s.Ctx.Err()
	}
}

type LateSuccessStream struct {
	grpc.ClientStream
	Ctx  context.Context
	Done chan struct{}
}

func (s *LateSuccessStream) Recv() (*executorpb.OpenSessionResponse, error) {
	<-s.Ctx.Done()
	close(s.Done)
	return &executorpb.OpenSessionResponse{Status: CompletedStatus(), ExecutionSessionId: "fake-execution-session"}, nil
}

type GatedStream struct {
	grpc.ClientStream
	Ctx      context.Context
	Started  chan struct{}
	Release  chan struct{}
	Finished chan struct{}
	Response *executorpb.OpenSessionResponse
	mu       sync.Mutex
	first    bool
}

func (s *GatedStream) Recv() (*executorpb.OpenSessionResponse, error) {
	s.mu.Lock()
	if s.first {
		s.mu.Unlock()
		<-s.Ctx.Done()
		return nil, s.Ctx.Err()
	}
	s.first = true
	s.mu.Unlock()
	close(s.Started)
	select {
	case <-s.Release:
		close(s.Finished)
		return s.Response, nil
	case <-s.Ctx.Done():
		close(s.Finished)
		return nil, s.Ctx.Err()
	}
}
