package adapter

import (
	"context"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
)

type unaryRequest interface {
	GetScope() *executorpb.ExecutionScope
	GetRequestId() string
	GetExecutionSessionId() string
}

func executeUnary[Native, Response any](
	ctx context.Context, a *Adapter, r unaryRequest,
	response *Response, responseStatus **executorpb.ExecutionStatus, responseFrames *[]*executorpb.ExecutionNativeFrame,
	method string, params any, populate func(*Native),
) (*Response, error) {
	sessionID := r.GetExecutionSessionId()
	if sessionID == "" {
		return nil, invalid("execution Session ID is required")
	}
	requestID := r.GetRequestId()
	outcome, frames, err := a.execute(ctx, r.GetScope(), requestID, sessionID, func(c *connection, frames *[]*executorpb.ExecutionNativeFrame) error {
		native, err := rpcResult[Native](c, requestID, method, params, frames)
		if err == nil && populate != nil {
			populate(native)
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	*responseStatus = outcome
	*responseFrames = frames
	return response, nil
}

func (a *Adapter) StartProcess(ctx context.Context, r *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
	if r.ProcessId == "" || len(r.Argv) == 0 || r.Argv[0] == "" || r.Cwd == "" {
		return nil, invalid("process ID, argv and cwd are required")
	}
	environment := r.Environment
	if environment == nil {
		environment = map[string]string{}
	}
	response := new(executorpb.StartProcessResponse)
	return executeUnary(
		ctx, a, r, response, &response.Status, &response.NativeFrames,
		"process/start", executorStart{
			ProcessID: r.ProcessId, Argv: r.Argv, Cwd: r.Cwd,
			EnvPolicy: environmentPolicy(r.EnvironmentPolicy), Env: environment,
			TTY: r.Tty, PipeStdin: r.PipeStdin, Arg0: r.Arg0,
		},
		func(native *executorStarted) {
			response.ProcessId = native.ProcessID
		},
	)
}

func (a *Adapter) ReadProcess(ctx context.Context, r *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
	if r.ProcessId == "" {
		return nil, invalid("process ID is required")
	}
	response := new(executorpb.ReadProcessResponse)
	return executeUnary(
		ctx, a, r, response, &response.Status, &response.NativeFrames,
		"process/read", executorRead{ProcessID: r.ProcessId, AfterSeq: r.AfterSeq, MaxBytes: int(r.MaxBytes), WaitMS: int(r.WaitMs)},
		func(native *executorReadResult) {
			response.NextSeq = native.NextSeq
			response.Exited = native.Exited
			response.Closed = native.Closed
			response.Failure = native.Failure
			if native.ExitCode != nil {
				code := int32(*native.ExitCode)
				response.ExitCode = &code
			}
			for _, chunk := range native.Chunks {
				response.Chunks = append(response.Chunks, &executorpb.ExecutionOutputChunk{Seq: chunk.Seq, Stream: chunk.Stream, Data: chunk.Chunk})
			}
		},
	)
}

func (a *Adapter) WriteProcess(ctx context.Context, r *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
	if r.ProcessId == "" || r.WriteId == "" {
		return nil, invalid("process ID and write ID are required")
	}
	data := r.Data
	if data == nil {
		data = []byte{}
	}
	response := new(executorpb.WriteProcessResponse)
	return executeUnary(
		ctx, a, r, response, &response.Status, &response.NativeFrames,
		"process/write", executorWrite{ProcessID: r.ProcessId, Chunk: data, WriteID: r.WriteId},
		func(native *executorWriteResult) {
			response.WriteStatus = native.Status
		},
	)
}

func (a *Adapter) SignalProcess(ctx context.Context, r *executorpb.SignalProcessRequest) (*executorpb.SignalProcessResponse, error) {
	if r.ProcessId == "" || r.Signal != "interrupt" {
		return nil, invalid("process ID and a supported signal are required")
	}
	response := new(executorpb.SignalProcessResponse)
	return executeUnary[executorEmpty](
		ctx, a, r, response, &response.Status, &response.NativeFrames,
		"process/signal", executorSignal{ProcessID: r.ProcessId, Signal: r.Signal}, nil,
	)
}

func (a *Adapter) TerminateProcess(ctx context.Context, r *executorpb.TerminateProcessRequest) (*executorpb.TerminateProcessResponse, error) {
	if r.ProcessId == "" {
		return nil, invalid("process ID is required")
	}
	response := new(executorpb.TerminateProcessResponse)
	return executeUnary(
		ctx, a, r, response, &response.Status, &response.NativeFrames,
		"process/terminate", executorTerminate{ProcessID: r.ProcessId},
		func(native *executorTerminateResult) {
			response.Running = native.Running
		},
	)
}

func (a *Adapter) GetFileMetadata(ctx context.Context, r *executorpb.GetFileMetadataRequest) (*executorpb.GetFileMetadataResponse, error) {
	if r.Path == "" {
		return nil, invalid("path is required")
	}
	response := new(executorpb.GetFileMetadataResponse)
	return executeUnary(
		ctx, a, r, response, &response.Status, &response.NativeFrames,
		"fs/getMetadata", executorFSGetMetadata{Path: r.Path},
		func(native *executorFSMetadata) {
			response.IsDirectory = native.IsDirectory
			response.IsFile = native.IsFile
			response.IsSymlink = native.IsSymlink
			response.Size = native.Size
		},
	)
}

func (a *Adapter) OpenFile(ctx context.Context, r *executorpb.OpenFileRequest) (*executorpb.OpenFileResponse, error) {
	if r.HandleId == "" || r.Path == "" {
		return nil, invalid("handle ID and path are required")
	}
	response := new(executorpb.OpenFileResponse)
	return executeUnary(
		ctx, a, r, response, &response.Status, &response.NativeFrames,
		"fs/open", executorFSOpen{HandleID: r.HandleId, Path: r.Path, Mode: "read"},
		func(native *executorFSOpened) {
			response.HandleId = native.HandleID
		},
	)
}

func (a *Adapter) ReadFile(ctx context.Context, r *executorpb.ReadFileRequest) (*executorpb.ReadFileResponse, error) {
	if r.HandleId == "" {
		return nil, invalid("handle ID is required")
	}
	response := new(executorpb.ReadFileResponse)
	return executeUnary(
		ctx, a, r, response, &response.Status, &response.NativeFrames,
		"fs/readBlock", executorFSReadBlock{HandleID: r.HandleId, Offset: r.Offset, Len: int(r.Length)},
		func(native *executorFSBlock) {
			response.Data = native.Chunk
			response.Eof = native.EOF
		},
	)
}

func (a *Adapter) CloseFile(ctx context.Context, r *executorpb.CloseFileRequest) (*executorpb.CloseFileResponse, error) {
	if r.HandleId == "" {
		return nil, invalid("handle ID is required")
	}
	response := new(executorpb.CloseFileResponse)
	return executeUnary[executorEmpty](
		ctx, a, r, response, &response.Status, &response.NativeFrames,
		"fs/close", executorFSClose{HandleID: r.HandleId}, nil,
	)
}

func environmentPolicy(policy *executorpb.ExecutionEnvironmentPolicy) *executorEnvPolicy {
	if policy == nil {
		return nil
	}
	exclude := policy.Exclude
	if exclude == nil {
		exclude = []string{}
	}
	set := policy.Set
	if set == nil {
		set = map[string]string{}
	}
	includeOnly := policy.IncludeOnly
	if includeOnly == nil {
		includeOnly = []string{}
	}
	return &executorEnvPolicy{Inherit: policy.Inherit, IgnoreDefaultExcludes: policy.IgnoreDefaultExcludes, Exclude: exclude, Set: set, IncludeOnly: includeOnly}
}
