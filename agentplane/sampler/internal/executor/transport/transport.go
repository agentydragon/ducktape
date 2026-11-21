package transport

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/outputbuffer"
	"google.golang.org/grpc"
)

type Method string

const (
	MethodInitialize       Method = "initialize"
	MethodProcessStart     Method = "process/start"
	MethodProcessRead      Method = "process/read"
	MethodProcessWrite     Method = "process/write"
	MethodProcessSignal    Method = "process/signal"
	MethodProcessTerminate Method = "process/terminate"
	MethodFSGetMetadata    Method = "fs/getMetadata"
	MethodFSOpen           Method = "fs/open"
	MethodFSReadBlock      Method = "fs/readBlock"
	MethodFSClose          Method = "fs/close"
	MethodSessionNotice    Method = "session_notification"
)

type Outcome string

const (
	Completed     Outcome = "completed"
	Rejected      Outcome = "rejected"
	NotDispatched Outcome = "not_dispatched"
	Unknown       Outcome = "unknown"
)

type FailureKind string

const (
	FailureTransport           FailureKind = "transport"
	FailureEnvironment         FailureKind = "environment"
	FailureScopeUnavailable    FailureKind = "scope_unavailable"
	FailureIncarnationFence    FailureKind = "incarnation_fence"
	FailureNotDispatched       FailureKind = "not_dispatched"
	FailureRejected            FailureKind = "rejected"
	FailureIncarnationReplaced FailureKind = "incarnation_replaced"
	FailureSessionReplaced     FailureKind = "session_replaced"
)

type Operation[T any] struct {
	Outcome            Outcome
	IntentID           string
	ExecutionSessionID string
	Evidence           json.RawMessage
	Value              T
	Failure            *Failure
}

type Failure struct {
	Kind                  FailureKind
	Method                Method
	Phase                 string
	IntentID              string
	ProcessID             string
	ExecutionSessionID    string
	AfterSequence         *uint64
	RemoteOutcome         Outcome
	RequestOutcome        *Outcome
	AutomaticReplay       bool
	EnvironmentState      string
	CurrentIncarnationUID string
	TransportError        string
	Output                *outputbuffer.Buffer
	Cause                 error
	Recorded              bool
	Evidence              json.RawMessage
	RejectionCode         int
	RejectionMessage      string
}

func (f *Failure) Error() string {
	switch f.Kind {
	case FailureTransport:
		operation := processDescription(f.ProcessID)
		if f.IntentID != "" {
			operation = " intent " + f.IntentID + operation
		}
		return fmt.Sprintf("typed execution %s %s failed%s: remote effect outcome is unknown; mutation was not replayed: %v", f.Method, f.Phase, operation, f.Cause)
	case FailureEnvironment:
		message := fmt.Sprintf("execution environment is %s; typed execution %s was not admitted", f.EnvironmentState, f.Method)
		if f.RemoteOutcome == Unknown {
			return message + "; the existing process outcome remains unknown and was not replayed"
		}
		return message + "; the request was not dispatched and was not retried"
	case FailureScopeUnavailable:
		return "cannot fence execution because the retained scope has no incarnation or Sandbox UID"
	case FailureIncarnationFence:
		if f.RemoteOutcome == Unknown {
			return fmt.Sprintf("execution incarnation fence rejected %s; the existing process outcome is unknown and was not replayed; current authorized incarnation UID is %s", f.Method, f.CurrentIncarnationUID)
		}
		return fmt.Sprintf("execution incarnation fence rejected %s before dispatch; current authorized incarnation UID is %s", f.Method, f.CurrentIncarnationUID)
	case FailureNotDispatched:
		return fmt.Sprintf("typed execution %s was not dispatched%s because the executor connection could not be established: %v; previous remote outcomes remain unknown and no mutation was replayed", f.Method, processDescription(f.ProcessID), f.Cause)
	case FailureRejected:
		return fmt.Sprintf("exec-server rejected %s (%d): %s", f.Method, f.RejectionCode, f.RejectionMessage)
	case FailureIncarnationReplaced:
		return "execution Sandbox changed incarnation; old execution handles were abandoned and a fresh session was opened. The requested operation was not dispatched"
	case FailureSessionReplaced:
		return fmt.Sprintf("retained exec-server session %q was definitively lost; its process handles were abandoned and a fresh session was opened. The requested operation was not dispatched and must not be retried automatically", f.ExecutionSessionID)
	}
	if f.Cause != nil {
		return f.Cause.Error()
	}
	return "executor operation failed"
}

func (f *Failure) Unwrap() error { return f.Cause }

type Journal interface {
	BeginExecutorIntent(context.Context, map[string]any) (string, error)
	FinishExecutorIntent(context.Context, string, any) error
	RecordExecutorNativeFrame(context.Context, NativeFrame) error
}

type NativeFrame struct {
	OperationID         string
	ExecutionSession    string
	Direction           string
	Payload             []byte
	PayloadDigestSHA256 string
	PayloadBytes        int
	OmitPayload         bool
}

type Bindings interface {
	CurrentSession(context.Context) (string, bool, error)
	BindInitialSession(context.Context, string, string, string, *string) error
	BindSession(context.Context, string, *string) error
	RetireLostSession(context.Context, string, string) error
	RebindIncarnation(context.Context, string, string, string, string) error
	RebindIncarnationWithSession(context.Context, string, string, string, string, *string) error
}

type ProcessChangeBroadcaster interface {
	SignalProcessChange()
}

type Connection interface {
	Close() error
}

type Shell struct {
	Name string
	Path string
}

type Config struct {
	Client            executorpb.ExecutorClient
	Connection        Connection
	Journal           Journal
	Bindings          Bindings
	ProcessChanges    ProcessChangeBroadcaster
	Scope             *executorpb.ExecutionScope
	Lifetime          context.Context
	InitialSessionID  string
	RPCTimeout        time.Duration
	InitializeTimeout time.Duration
	ReconnectTimeout  time.Duration
}

type Transport struct {
	client         executorpb.ExecutorClient
	connection     Connection
	journal        Journal
	bindings       Bindings
	processChanges ProcessChangeBroadcaster

	scopeMu sync.RWMutex
	scope   *executorpb.ExecutionScope
	shell   *Shell

	rpcMu                sync.Mutex
	sessionStateMu       sync.Mutex
	reconnectRequired    bool
	lostSessionConfirmed bool
	sessionGeneration    uint64
	sessionError         error
	closing              bool

	rpcTimeout        time.Duration
	initializeTimeout time.Duration
	reconnectTimeout  time.Duration
	lifetime          context.Context
	sessionStream     grpc.ServerStreamingClient[executorpb.OpenSessionResponse]
	sessionCancel     context.CancelFunc
	sessionDone       chan struct{}
	sessionID         string
}

func New(config Config) *Transport {
	initialSessionID := config.InitialSessionID
	return &Transport{
		client: config.Client, connection: config.Connection, journal: config.Journal,
		bindings: config.Bindings, processChanges: config.ProcessChanges,
		scope: config.Scope, lifetime: config.Lifetime, sessionID: initialSessionID,
		reconnectRequired: initialSessionID != "",
		rpcTimeout:        config.RPCTimeout, initializeTimeout: config.InitializeTimeout,
		reconnectTimeout: config.ReconnectTimeout,
	}
}

func (t *Transport) ShellSnapshot() *Shell {
	t.rpcMu.Lock()
	defer t.rpcMu.Unlock()
	if t.shell == nil {
		return nil
	}
	shell := *t.shell
	return &shell
}

func failureFromError(err error) *Failure {
	failure, _ := err.(*Failure)
	return failure
}

// FindFailure retains error-tree matching across wrapped and joined failures.
// Settlement searches for admission failures before the remote failure that caused them.
func FindFailure(err error, kinds ...FailureKind) *Failure {
	if failure, ok := err.(*Failure); ok {
		for _, kind := range kinds {
			if failure.Kind == kind {
				return failure
			}
		}
	}
	switch wrapped := err.(type) {
	case interface{ Unwrap() []error }:
		for _, cause := range wrapped.Unwrap() {
			if failure := FindFailure(cause, kinds...); failure != nil {
				return failure
			}
		}
	case interface{ Unwrap() error }:
		return FindFailure(wrapped.Unwrap(), kinds...)
	}
	return nil
}

func processDescription(id string) string {
	if id == "" {
		return ""
	}
	return " for process " + id
}
