package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// Codex rust-v0.162.1 exec-server-protocol/src/protocol.rs and rpc.rs own these wire shapes.
type executorRequest[P any] struct {
	ID     string `json:"id,omitempty"`
	Method string `json:"method"`
	Params P      `json:"params"`
}

type executorReply struct {
	ID     *string           `json:"id"`
	Method string            `json:"method"`
	Result json.RawMessage   `json:"result"`
	Error  *executorRPCError `json:"error"`
}

type executorRPCError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

// executorRejectedError is an exec-server JSON-RPC error reply: the request had no effect.
type executorRejectedError struct {
	method  string
	code    int
	message string
}

func (e *executorRejectedError) Error() string {
	return fmt.Sprintf("exec-server rejected %s (%d): %s", e.method, e.code, e.message)
}

type executorInitialize struct {
	ClientName      string  `json:"clientName"`
	ResumeSessionID *string `json:"resumeSessionId"`
}

type (
	executorInitialized struct {
		SessionID       string                   `json:"sessionId"`
		EnvironmentInfo *executorEnvironmentInfo `json:"environmentInfo"`
	}
	executorEnvironmentInfo struct {
		Shell execShell `json:"shell"`
	}
	executorEmpty   struct{}
	executorStarted struct {
		ProcessID string `json:"processId"`
	}
	executorStart struct {
		ProcessID string             `json:"processId"`
		Argv      []string           `json:"argv"`
		Cwd       string             `json:"cwd"`
		EnvPolicy *executorEnvPolicy `json:"envPolicy"`
		Env       map[string]string  `json:"env"`
		TTY       bool               `json:"tty"`
		PipeStdin bool               `json:"pipeStdin"`
		Arg0      *string            `json:"arg0"`
	}
)

// executorEnvPolicy builds a child environment on the executor; `env` is applied over it.
// Codex rust-v0.162.1 exec-server-protocol/src/protocol.rs ExecEnvPolicy.
type executorEnvPolicy struct {
	Inherit               string            `json:"inherit"`
	IgnoreDefaultExcludes bool              `json:"ignoreDefaultExcludes"`
	Exclude               []string          `json:"exclude"`
	Set                   map[string]string `json:"set"`
	IncludeOnly           []string          `json:"includeOnly"`
}

type executorRead struct {
	ProcessID string  `json:"processId"`
	AfterSeq  *uint64 `json:"afterSeq"`
	MaxBytes  int     `json:"maxBytes"`
	WaitMS    int     `json:"waitMs"`
}

type executorChunk struct {
	Seq    uint64 `json:"seq"`
	Stream string `json:"stream"`
	Chunk  []byte `json:"chunk"`
}

type executorReadResult struct {
	Chunks   []executorChunk `json:"chunks"`
	NextSeq  uint64          `json:"nextSeq"`
	Exited   bool            `json:"exited"`
	ExitCode *int            `json:"exitCode"`
	Closed   bool            `json:"closed"`
	Failure  *string         `json:"failure"`
}

func (r *executorReadResult) UnmarshalJSON(data []byte) error {
	type readResult executorReadResult
	var result readResult
	if err := decodeProtocolObject(data, &result); err != nil {
		return err
	}
	if result.Chunks == nil {
		return errors.New("process/read result requires a chunks array")
	}
	for _, chunk := range result.Chunks {
		if chunk.Chunk == nil {
			return errors.New("process/read chunk requires a base64 payload")
		}
	}
	*r = executorReadResult(result)
	return nil
}

func (r executorReadResult) output() []byte {
	var output []byte
	for _, chunk := range r.Chunks {
		output = append(output, chunk.Chunk...)
	}
	return output
}

func (r executorReadResult) uncertain() bool { return r.Failure != nil || r.Closed && !r.Exited }

type executorWrite struct {
	ProcessID string `json:"processId"`
	Chunk     []byte `json:"chunk"`
	WriteID   string `json:"writeId"`
}
type (
	executorWriteResult struct {
		Status string `json:"status"`
	}
	executorSignal struct {
		ProcessID string `json:"processId"`
		Signal    string `json:"signal"`
	}
)

type (
	executorTerminate struct {
		ProcessID string `json:"processId"`
	}
	executorTerminateResult struct {
		Running bool `json:"running"`
	}
)

// Decode one complete JSON object. Raw payloads remain available to the evidence journal.
func decodeProtocolObject(data []byte, value any) error {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return errors.New("protocol payload must be a JSON object")
	}
	decoder := json.NewDecoder(bytes.NewReader(trimmed))
	decoder.UseNumber()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("protocol payload has trailing JSON")
	}
	return nil
}

// Evidence uses JSON objects, while transport and execution use concrete protocol types.
func protocolObject(value any) (map[string]any, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var object map[string]any
	if err := decodeProtocolObject(data, &object); err != nil {
		return nil, err
	}
	return object, nil
}

func executorRPC[R any, P any](ctx context.Context, e *Executor, method string, params P) (*R, error) {
	raw, err := e.rpc(ctx, method, params)
	if err != nil {
		return nil, err
	}
	var result R
	if err := decodeProtocolObject(raw, &result); err != nil {
		return nil, fmt.Errorf("decode exec-server %s result: %w", method, err)
	}
	return &result, nil
}
