// Package typedbackend exposes native execution to Runtime's local transport.
// Runtime remains responsible for external admission and incarnation checks.
package typedbackend

import (
	"context"
	"errors"
	"math"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type Server struct {
	pb.UnimplementedExecutorServer
	native *native.Server
}

func New(s *native.Server) *Server { return &Server{native: s} }
func completed() *pb.ExecutionStatus {
	return &pb.ExecutionStatus{Outcome: pb.ExecutionStatus_OUTCOME_COMPLETED}
}

func rejected(err error) *pb.ExecutionStatus {
	if err == nil {
		return completed()
	}
	return &pb.ExecutionStatus{Outcome: pb.ExecutionStatus_OUTCOME_REJECTED, Message: err.Error()}
}

// A native failure after invocation can follow a partial mutation. Do not claim
// that it had no effect merely because a local error was returned.
func uncertain(err error) *pb.ExecutionStatus {
	if err == nil {
		return completed()
	}
	return &pb.ExecutionStatus{Outcome: pb.ExecutionStatus_OUTCOME_UNKNOWN, Message: err.Error()}
}

func (s *Server) session(id string) (*native.Session, error) {
	if id == "" {
		return nil, errors.New("execution session ID required")
	}
	return s.native.Session(id)
}

func (s *Server) OpenSession(r *pb.OpenSessionRequest, stream pb.Executor_OpenSessionServer) error {
	if r.GetRequestId() == "" {
		return status.Error(codes.InvalidArgument, "request ID required")
	}
	sess, err := s.native.OpenSession(r.GetResumeSessionId())
	if err == nil {
		defer s.native.Detach(sess.ID())
	}
	response := &pb.OpenSessionResponse{Status: rejected(err)}
	if errors.Is(err, native.ErrUnknownSession) && r.GetResumeSessionId() != "" {
		code := int32(-32600)
		response.Status.Message = "unknown session id " + r.GetResumeSessionId()
		response.Status.UpstreamCode = &code
	}
	if err == nil {
		response.ExecutionSessionId = sess.ID()
		response.Shell = &pb.ExecutionShell{Name: "bash", Path: "/bin/bash"}
	}
	var changed <-chan struct{}
	if sess != nil {
		changed = sess.Changes()
	}
	if err := stream.Send(response); err != nil {
		return err
	}
	if sess == nil {
		return nil
	}
	for {
		select {
		case <-stream.Context().Done():
			return nil
		case <-changed:
			changed = sess.Changes()
			if err := stream.Send(&pb.OpenSessionResponse{ExecutionSessionId: sess.ID(), ProcessChanged: true}); err != nil {
				return err
			}
		}
	}
}

func (s *Server) StartProcess(ctx context.Context, r *pb.StartProcessRequest) (*pb.StartProcessResponse, error) {
	sess, err := s.session(r.GetExecutionSessionId())
	if err != nil {
		return &pb.StartProcessResponse{Status: rejected(err)}, nil
	}
	var policy *native.EnvPolicy
	if p := r.GetEnvironmentPolicy(); p != nil {
		policy = &native.EnvPolicy{Inherit: p.Inherit, IgnoreDefaultExcludes: p.IgnoreDefaultExcludes, Exclude: p.Exclude, Set: p.Set, IncludeOnly: p.IncludeOnly}
	}
	env, err := native.BuildEnvironment(policy, r.Environment)
	if err != nil {
		return &pb.StartProcessResponse{Status: rejected(err)}, nil
	}
	err = sess.Start(native.StartRequest{ProcessID: r.ProcessId, Argv: r.Argv, Cwd: r.Cwd, Environment: env, TTY: r.Tty, PipeStdin: r.PipeStdin, Arg0: r.Arg0})

	res := &pb.StartProcessResponse{Status: uncertain(err)}
	if err == nil {
		res.ProcessId = r.ProcessId
	}
	return res, nil
}

func (s *Server) ReadProcess(ctx context.Context, r *pb.ReadProcessRequest) (*pb.ReadProcessResponse, error) {
	sess, err := s.session(r.GetExecutionSessionId())
	if err != nil {
		return &pb.ReadProcessResponse{Status: rejected(err)}, nil
	}
	v, err := sess.Read(ctx, r.ProcessId, r.AfterSeq, int(r.MaxBytes), time.Duration(r.WaitMs)*time.Millisecond)
	out := &pb.ReadProcessResponse{Status: rejected(err), NextSeq: v.NextSeq, Exited: v.Exited, Closed: v.Closed, Failure: v.Failure}
	if v.ExitCode != nil {
		code := int32(*v.ExitCode)
		out.ExitCode = &code
	}
	for _, c := range v.Chunks {
		out.Chunks = append(out.Chunks, &pb.ExecutionOutputChunk{Seq: c.Seq, Stream: c.Stream, Data: c.Data})
	}
	return out, nil
}

func (s *Server) WriteProcess(ctx context.Context, r *pb.WriteProcessRequest) (*pb.WriteProcessResponse, error) {
	sess, err := s.session(r.GetExecutionSessionId())
	if err != nil {
		return &pb.WriteProcessResponse{Status: rejected(err)}, nil
	}
	v, err := sess.WriteContext(ctx, r.ProcessId, r.WriteId, r.Data)
	return &pb.WriteProcessResponse{Status: uncertain(err), WriteStatus: v}, nil
}

func (s *Server) SignalProcess(ctx context.Context, r *pb.SignalProcessRequest) (*pb.SignalProcessResponse, error) {
	sess, err := s.session(r.GetExecutionSessionId())
	if err == nil {
		if r.Signal != "interrupt" {
			err = errors.New("unsupported signal")
		} else {
			err = sess.Signal(r.ProcessId)
		}
	}
	return &pb.SignalProcessResponse{Status: uncertain(err)}, nil
}

func (s *Server) TerminateProcess(ctx context.Context, r *pb.TerminateProcessRequest) (*pb.TerminateProcessResponse, error) {
	sess, err := s.session(r.GetExecutionSessionId())
	var running bool
	if err == nil {
		running, err = sess.Terminate(r.ProcessId)
	}
	return &pb.TerminateProcessResponse{Status: uncertain(err), Running: running}, nil
}

func (s *Server) GetFileMetadata(ctx context.Context, r *pb.GetFileMetadataRequest) (*pb.GetFileMetadataResponse, error) {
	sess, err := s.session(r.GetExecutionSessionId())
	if err != nil {
		return &pb.GetFileMetadataResponse{Status: rejected(err)}, nil
	}
	v, err := sess.Metadata(r.Path)
	return &pb.GetFileMetadataResponse{Status: rejected(err), IsDirectory: v.IsDirectory, IsFile: v.IsFile, IsSymlink: v.IsSymlink, Size: v.Size}, nil
}

func (s *Server) OpenFile(ctx context.Context, r *pb.OpenFileRequest) (*pb.OpenFileResponse, error) {
	sess, err := s.session(r.GetExecutionSessionId())
	if err == nil {
		err = sess.OpenFile(r.HandleId, r.Path)
	}
	out := &pb.OpenFileResponse{Status: rejected(err)}
	if err == nil {
		out.HandleId = r.HandleId
	}
	return out, nil
}

func (s *Server) ReadFile(ctx context.Context, r *pb.ReadFileRequest) (*pb.ReadFileResponse, error) {
	sess, err := s.session(r.GetExecutionSessionId())
	if err != nil {
		return &pb.ReadFileResponse{Status: rejected(err)}, nil
	}
	if r.Offset > math.MaxInt64 {
		return &pb.ReadFileResponse{Status: rejected(errors.New("offset out of range"))}, nil
	}
	data, eof, err := sess.ReadFile(r.HandleId, int64(r.Offset), int(r.Length))
	return &pb.ReadFileResponse{Status: rejected(err), Data: data, Eof: eof}, nil
}

func (s *Server) CloseFile(ctx context.Context, r *pb.CloseFileRequest) (*pb.CloseFileResponse, error) {
	sess, err := s.session(r.GetExecutionSessionId())
	if err == nil {
		err = sess.CloseFile(r.HandleId)
	}
	return &pb.CloseFileResponse{Status: rejected(err)}, nil
}
