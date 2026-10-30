package execution

import (
	"context"

	executionpb "github.com/agentydragon/ducktape/agentplane/proto/executionpb"
)

func (a *Adapter) StartProcess(ctx context.Context, r *executionpb.StartProcessRequest) (*executionpb.StartProcessResponse, error) {
	if r.ProcessId == "" || len(r.Argv) == 0 || r.Argv[0] == "" || r.Cwd == "" {
		return nil, invalid("process ID, argv and cwd are required")
	}
	if r.ExecutionSessionId == "" {
		return nil, invalid("execution Session ID is required")
	}
	response := new(executionpb.StartProcessResponse)
	environment := r.Environment
	if environment == nil {
		environment = map[string]string{}
	}
	outcome, frames, err := a.execute(ctx, r.Scope, r.RequestId, r.ExecutionSessionId, func(c *connection, frames *[]*executionpb.ExecutionNativeFrame) error {
		native, err := rpcResult[executorStarted](c, r.RequestId, "process/start", executorStart{ProcessID: r.ProcessId, Argv: r.Argv, Cwd: r.Cwd, EnvPolicy: environmentPolicy(r.EnvironmentPolicy), Env: environment, TTY: r.Tty, PipeStdin: r.PipeStdin, Arg0: r.Arg0}, frames)
		if err != nil {
			return err
		}
		response.ProcessId = native.ProcessID
		return nil
	})
	if err != nil {
		return nil, err
	}
	response.Status = outcome
	response.NativeFrames = frames
	return response, nil
}

func (a *Adapter) ReadProcess(ctx context.Context, r *executionpb.ReadProcessRequest) (*executionpb.ReadProcessResponse, error) {
	if r.ProcessId == "" {
		return nil, invalid("process ID is required")
	}
	if r.ExecutionSessionId == "" {
		return nil, invalid("execution Session ID is required")
	}
	response := new(executionpb.ReadProcessResponse)
	outcome, frames, err := a.execute(ctx, r.Scope, r.RequestId, r.ExecutionSessionId, func(c *connection, frames *[]*executionpb.ExecutionNativeFrame) error {
		native, err := rpcResult[executorReadResult](c, r.RequestId, "process/read", executorRead{ProcessID: r.ProcessId, AfterSeq: r.AfterSeq, MaxBytes: int(r.MaxBytes), WaitMS: int(r.WaitMs)}, frames)
		if err != nil {
			return err
		}
		response.NextSeq = native.NextSeq
		response.Exited = native.Exited
		response.Closed = native.Closed
		response.Failure = native.Failure
		if native.ExitCode != nil {
			code := int32(*native.ExitCode)
			response.ExitCode = &code
		}
		for _, chunk := range native.Chunks {
			response.Chunks = append(response.Chunks, &executionpb.ExecutionOutputChunk{Seq: chunk.Seq, Stream: chunk.Stream, Data: chunk.Chunk})
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	response.Status = outcome
	response.NativeFrames = frames
	return response, nil
}

func (a *Adapter) WriteProcess(ctx context.Context, r *executionpb.WriteProcessRequest) (*executionpb.WriteProcessResponse, error) {
	if r.ProcessId == "" || r.WriteId == "" {
		return nil, invalid("process ID and write ID are required")
	}
	if r.ExecutionSessionId == "" {
		return nil, invalid("execution Session ID is required")
	}
	response := new(executionpb.WriteProcessResponse)
	outcome, frames, err := a.execute(ctx, r.Scope, r.RequestId, r.ExecutionSessionId, func(c *connection, frames *[]*executionpb.ExecutionNativeFrame) error {
		native, err := rpcResult[executorWriteResult](c, r.RequestId, "process/write", executorWrite{ProcessID: r.ProcessId, Chunk: r.Data, WriteID: r.WriteId}, frames)
		if err != nil {
			return err
		}
		response.WriteStatus = native.Status
		return nil
	})
	if err != nil {
		return nil, err
	}
	response.Status = outcome
	response.NativeFrames = frames
	return response, nil
}

func (a *Adapter) SignalProcess(ctx context.Context, r *executionpb.SignalProcessRequest) (*executionpb.SignalProcessResponse, error) {
	if r.ProcessId == "" || r.Signal != "interrupt" {
		return nil, invalid("process ID and a supported signal are required")
	}
	if r.ExecutionSessionId == "" {
		return nil, invalid("execution Session ID is required")
	}
	response := new(executionpb.SignalProcessResponse)
	outcome, frames, err := a.execute(ctx, r.Scope, r.RequestId, r.ExecutionSessionId, func(c *connection, frames *[]*executionpb.ExecutionNativeFrame) error {
		_, err := rpcResult[executorEmpty](c, r.RequestId, "process/signal", executorSignal{ProcessID: r.ProcessId, Signal: r.Signal}, frames)
		if err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	response.Status = outcome
	response.NativeFrames = frames
	return response, nil
}

func (a *Adapter) TerminateProcess(ctx context.Context, r *executionpb.TerminateProcessRequest) (*executionpb.TerminateProcessResponse, error) {
	if r.ProcessId == "" {
		return nil, invalid("process ID is required")
	}
	if r.ExecutionSessionId == "" {
		return nil, invalid("execution Session ID is required")
	}
	response := new(executionpb.TerminateProcessResponse)
	outcome, frames, err := a.execute(ctx, r.Scope, r.RequestId, r.ExecutionSessionId, func(c *connection, frames *[]*executionpb.ExecutionNativeFrame) error {
		native, err := rpcResult[executorTerminateResult](c, r.RequestId, "process/terminate", executorTerminate{ProcessID: r.ProcessId}, frames)
		if err != nil {
			return err
		}
		response.Running = native.Running
		return nil
	})
	if err != nil {
		return nil, err
	}
	response.Status = outcome
	response.NativeFrames = frames
	return response, nil
}

func (a *Adapter) GetFileMetadata(ctx context.Context, r *executionpb.GetFileMetadataRequest) (*executionpb.GetFileMetadataResponse, error) {
	if r.Path == "" {
		return nil, invalid("path is required")
	}
	if r.ExecutionSessionId == "" {
		return nil, invalid("execution Session ID is required")
	}
	response := new(executionpb.GetFileMetadataResponse)
	outcome, frames, err := a.execute(ctx, r.Scope, r.RequestId, r.ExecutionSessionId, func(c *connection, frames *[]*executionpb.ExecutionNativeFrame) error {
		native, err := rpcResult[executorFSMetadata](c, r.RequestId, "fs/getMetadata", executorFSGetMetadata{Path: r.Path}, frames)
		if err != nil {
			return err
		}
		response.IsDirectory = native.IsDirectory
		response.IsFile = native.IsFile
		response.IsSymlink = native.IsSymlink
		response.Size = native.Size
		return nil
	})
	if err != nil {
		return nil, err
	}
	response.Status = outcome
	response.NativeFrames = frames
	return response, nil
}

func (a *Adapter) OpenFile(ctx context.Context, r *executionpb.OpenFileRequest) (*executionpb.OpenFileResponse, error) {
	if r.HandleId == "" || r.Path == "" {
		return nil, invalid("handle ID and path are required")
	}
	if r.ExecutionSessionId == "" {
		return nil, invalid("execution Session ID is required")
	}
	response := new(executionpb.OpenFileResponse)
	outcome, frames, err := a.execute(ctx, r.Scope, r.RequestId, r.ExecutionSessionId, func(c *connection, frames *[]*executionpb.ExecutionNativeFrame) error {
		native, err := rpcResult[executorFSOpened](c, r.RequestId, "fs/open", executorFSOpen{HandleID: r.HandleId, Path: r.Path, Mode: "read"}, frames)
		if err != nil {
			return err
		}
		response.HandleId = native.HandleID
		return nil
	})
	if err != nil {
		return nil, err
	}
	response.Status = outcome
	response.NativeFrames = frames
	return response, nil
}

func (a *Adapter) ReadFile(ctx context.Context, r *executionpb.ReadFileRequest) (*executionpb.ReadFileResponse, error) {
	if r.HandleId == "" {
		return nil, invalid("handle ID is required")
	}
	if r.ExecutionSessionId == "" {
		return nil, invalid("execution Session ID is required")
	}
	response := new(executionpb.ReadFileResponse)
	outcome, frames, err := a.execute(ctx, r.Scope, r.RequestId, r.ExecutionSessionId, func(c *connection, frames *[]*executionpb.ExecutionNativeFrame) error {
		native, err := rpcResult[executorFSBlock](c, r.RequestId, "fs/readBlock", executorFSReadBlock{HandleID: r.HandleId, Offset: r.Offset, Len: int(r.Length)}, frames)
		if err != nil {
			return err
		}
		response.Data = native.Chunk
		response.Eof = native.EOF
		return nil
	})
	if err != nil {
		return nil, err
	}
	response.Status = outcome
	response.NativeFrames = frames
	return response, nil
}

func (a *Adapter) CloseFile(ctx context.Context, r *executionpb.CloseFileRequest) (*executionpb.CloseFileResponse, error) {
	if r.HandleId == "" {
		return nil, invalid("handle ID is required")
	}
	if r.ExecutionSessionId == "" {
		return nil, invalid("execution Session ID is required")
	}
	response := new(executionpb.CloseFileResponse)
	outcome, frames, err := a.execute(ctx, r.Scope, r.RequestId, r.ExecutionSessionId, func(c *connection, frames *[]*executionpb.ExecutionNativeFrame) error {
		_, err := rpcResult[executorEmpty](c, r.RequestId, "fs/close", executorFSClose{HandleID: r.HandleId}, frames)
		if err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	response.Status = outcome
	response.NativeFrames = frames
	return response, nil
}

func environmentPolicy(policy *executionpb.ExecutionEnvironmentPolicy) *executorEnvPolicy {
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
