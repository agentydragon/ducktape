package transport

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/jsonwire"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/outputbuffer"
)

type EnvironmentPolicy struct {
	Inherit               string
	IgnoreDefaultExcludes bool
	Exclude               []string
	Set                   map[string]string
	IncludeOnly           []string
}

type StartRequest struct {
	ProcessID string
	Argv      []string
	Cwd       string
	EnvPolicy *EnvironmentPolicy
	Env       map[string]string
	TTY       bool
	PipeStdin bool
	Arg0      *string
}

type Started struct {
	ProcessID string `json:"processId"`
}

type ReadRequest struct {
	ProcessID      string
	AfterSeq       *uint64
	MaxBytes       int
	WaitMS         int
	AttemptTimeout time.Duration
	ObservedOutput *outputbuffer.Buffer
}

type Chunk struct {
	Seq    uint64 `json:"seq"`
	Stream string `json:"stream"`
	Chunk  []byte `json:"chunk"`
}

type ReadResult struct {
	Chunks   []Chunk `json:"chunks"`
	NextSeq  uint64  `json:"nextSeq"`
	Exited   bool    `json:"exited"`
	ExitCode *int    `json:"exitCode"`
	Closed   bool    `json:"closed"`
	Failure  *string `json:"failure"`
}

type WriteRequest struct {
	ProcessID string
	Chunk     []byte
	WriteID   string
}

type WriteResult struct {
	Status string `json:"status"`
}

type SignalRequest struct {
	ProcessID string
	Signal    string
}

type TerminateRequest struct{ ProcessID string }

type Terminated struct {
	Running bool `json:"running"`
}

type GetMetadataRequest struct{ Path string }

type Metadata struct {
	IsDirectory bool   `json:"isDirectory"`
	IsFile      bool   `json:"isFile"`
	IsSymlink   bool   `json:"isSymlink"`
	Size        uint64 `json:"size"`
}

type OpenFileRequest struct {
	HandleID string
	Path     string
	Mode     string
}

type OpenedFile struct {
	HandleID string `json:"handleId"`
}

type ReadBlockRequest struct {
	HandleID string
	Offset   uint64
	Len      int
}

type Block struct {
	Chunk []byte `json:"chunk"`
	EOF   bool   `json:"eof"`
}

type CloseFileRequest struct{ HandleID string }

func (t *Transport) EnsureSession(ctx context.Context) (Operation[struct{}], error) {
	t.rpcMu.Lock()
	defer t.rpcMu.Unlock()
	if err := t.ensureSessionLocked(ctx, MethodProcessStart, ""); err != nil {
		failure := failureFromError(err)
		if failure == nil {
			return Operation[struct{}]{}, err
		}
		return failureOperation[struct{}](failure), nil
	}
	return Operation[struct{}]{Outcome: Completed, ExecutionSessionID: t.sessionID}, nil
}

func (t *Transport) Start(ctx context.Context, request StartRequest) (Operation[Started], error) {
	var policy *executorEnvPolicy
	if request.EnvPolicy != nil {
		policy = &executorEnvPolicy{
			Inherit: request.EnvPolicy.Inherit, IgnoreDefaultExcludes: request.EnvPolicy.IgnoreDefaultExcludes,
			Exclude: request.EnvPolicy.Exclude, Set: request.EnvPolicy.Set, IncludeOnly: request.EnvPolicy.IncludeOnly,
		}
	}
	return callTyped[Started](t, ctx, MethodProcessStart, executorStart{
		ProcessID: request.ProcessID, Argv: request.Argv, Cwd: request.Cwd, EnvPolicy: policy,
		Env: request.Env, TTY: request.TTY, PipeStdin: request.PipeStdin, Arg0: request.Arg0,
	})
}

func (t *Transport) Read(ctx context.Context, request ReadRequest) (Operation[ReadResult], error) {
	if request.AttemptTimeout > 0 {
		ctx = context.WithValue(ctx, executorReadAttemptTimeoutKey{}, request.AttemptTimeout)
	}
	return callTyped[ReadResult](t, ctx, MethodProcessRead, executorRead{
		ProcessID: request.ProcessID, AfterSeq: request.AfterSeq, MaxBytes: request.MaxBytes,
		WaitMS: request.WaitMS, ObservedOutput: request.ObservedOutput,
	})
}

func (t *Transport) Write(ctx context.Context, request WriteRequest) (Operation[WriteResult], error) {
	return callTyped[WriteResult](t, ctx, MethodProcessWrite, executorWrite{ProcessID: request.ProcessID, Chunk: request.Chunk, WriteID: request.WriteID})
}

func (t *Transport) Signal(ctx context.Context, request SignalRequest) (Operation[struct{}], error) {
	return callTyped[struct{}](t, ctx, MethodProcessSignal, executorSignal{ProcessID: request.ProcessID, Signal: request.Signal})
}

func (t *Transport) Terminate(ctx context.Context, request TerminateRequest) (Operation[Terminated], error) {
	return callTyped[Terminated](t, ctx, MethodProcessTerminate, executorTerminate{ProcessID: request.ProcessID})
}

func (t *Transport) GetMetadata(ctx context.Context, request GetMetadataRequest) (Operation[Metadata], error) {
	return callTyped[Metadata](t, ctx, MethodFSGetMetadata, executorFSGetMetadata{Path: request.Path})
}

func (t *Transport) OpenFile(ctx context.Context, request OpenFileRequest) (Operation[OpenedFile], error) {
	return callTyped[OpenedFile](t, ctx, MethodFSOpen, executorFSOpen{HandleID: request.HandleID, Path: request.Path, Mode: request.Mode})
}

func (t *Transport) ReadBlock(ctx context.Context, request ReadBlockRequest) (Operation[Block], error) {
	return callTyped[Block](t, ctx, MethodFSReadBlock, executorFSReadBlock{HandleID: request.HandleID, Offset: request.Offset, Len: request.Len})
}

func (t *Transport) CloseFile(ctx context.Context, request CloseFileRequest) (Operation[struct{}], error) {
	return callTyped[struct{}](t, ctx, MethodFSClose, executorFSClose{HandleID: request.HandleID})
}

func callTyped[T any](t *Transport, ctx context.Context, method Method, request any) (Operation[T], error) {
	raw, err := t.rpc(ctx, method, request)
	if err != nil {
		return Operation[T]{}, err
	}
	result := Operation[T]{
		Outcome: raw.Outcome, IntentID: raw.IntentID, ExecutionSessionID: raw.ExecutionSessionID,
		Evidence: raw.Evidence, Failure: raw.Failure,
	}
	if raw.Outcome != Completed {
		return result, nil
	}
	if err := jsonwire.DecodeObject(raw.Value, &result.Value); err != nil {
		return Operation[T]{}, fmt.Errorf("decode typed execution %s result: %w", method, err)
	}
	return result, nil
}

func failureOperation[T any](failure *Failure) Operation[T] {
	outcome := Unknown
	switch failure.Kind {
	case FailureEnvironment, FailureScopeUnavailable, FailureIncarnationFence, FailureNotDispatched, FailureIncarnationReplaced, FailureSessionReplaced:
		outcome = NotDispatched
	case FailureRejected:
		outcome = Rejected
	case FailureTransport:
		outcome = Unknown
	}
	return Operation[T]{
		Outcome: outcome, IntentID: failure.IntentID,
		ExecutionSessionID: failure.ExecutionSessionID, Evidence: failure.Evidence, Failure: failure,
	}
}

func marshalEvidence(evidence any) (json.RawMessage, error) {
	encoded, err := json.Marshal(evidence)
	if err != nil {
		return nil, fmt.Errorf("encode executor operation evidence: %w", err)
	}
	return encoded, nil
}
