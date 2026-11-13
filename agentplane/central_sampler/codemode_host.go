package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

// CodeModeHost binds sampler ownership and tool execution to the shared host.
type CodeModeHost struct {
	*codemode.Manager
	store *Store
}

func NewCodeModeHost(parent context.Context, settings Settings, store *Store) *CodeModeHost {
	return &CodeModeHost{Manager: codemode.NewManager(parent, settings.CodeModeHost, settings.Logs, time.Duration(settings.RPCTimeoutSeconds)*time.Second), store: store}
}

func (m *CodeModeHost) OpenSession(ctx context.Context, conversationID string, owner Owner, executor *Executor) (*CodeMode, error) {
	if executor == nil {
		return nil, errors.New("code-mode executor is required")
	}
	code := &CodeMode{store: m.store, owner: owner, executor: executor}
	var journal codemode.Journal
	if m.store != nil {
		journal = codeJournal{store: m.store, owner: owner}
	}
	session, err := m.Manager.OpenSession(ctx, conversationID, journal, code.delegate)
	if err != nil {
		return nil, err
	}
	code.Session = session
	return code, nil
}

// The lease owner is captured once for this binding. Every journal write retains Store's fencing.
type codeJournal struct {
	store *Store
	owner Owner
}

func (j codeJournal) Epoch(ctx context.Context) (int, error) {
	return j.store.CodeSessionEpoch(ctx, j.owner)
}

func (j codeJournal) StampHost(ctx context.Context, instanceID string) error {
	return j.store.StampCodeHostInstance(ctx, j.owner, instanceID)
}

func (j codeJournal) ProtocolEvent(ctx context.Context, operationID string, phase eventpb.ExecutionProgress_Phase, object map[string]any) error {
	return j.store.ProtocolEvent(ctx, j.owner, operationID, eventpb.ExecutionProgress_KIND_JAVASCRIPT, phase, object)
}

func (j codeJournal) CellStarted(ctx context.Context, cellID string) error {
	return j.store.CellResult(ctx, j.owner, cellID, protocol.CellLive)
}

func (r *CodeMode) delegate(ctx context.Context, session *codemode.Session, request codemode.Delegate) (codemode.DelegateResult, codemode.DelegateResult, error) {
	var empty codemode.DelegateResult
	if request.Type == "notification/send" {
		// Acknowledge nonblank notifications only after they are durably queued.
		result := codemode.DelegateResult{Status: "ok", Value: &codemode.ToolResult{Type: "notification/delivered"}}
		if strings.TrimSpace(request.Text) != "" {
			err := r.store.QueueNotification(ctx, r.owner, request.CallID, session.CellHandle(request.CellID), request.Text)
			if errors.Is(err, protocol.ErrPrecondition) {
				result = codemode.DelegateResult{Status: "error", Message: fmt.Sprintf("failed to inject exec notify message for cell %s: no active turn", request.CellID)}
			} else if err != nil {
				return empty, empty, err
			}
		}
		return result, result, nil
	}
	if request.Type != "tool/invoke" {
		return empty, empty, fmt.Errorf("unsupported code-mode delegate type %q", request.Type)
	}
	invocation := request.Invocation
	if invocation.ToolName.Name == "" || invocation.ToolName.Namespace != nil {
		return empty, empty, errors.New("code-mode tool invocation has no supported tool name")
	}
	requestJournal, err := protocol.Object(invocation)
	if err != nil {
		return empty, empty, err
	}
	if invocation.CellID != "" {
		requestJournal["cell_id"] = session.CellHandle(invocation.CellID)
	}
	intent, err := r.store.RecordNested(ctx, r.owner, IntentTool, requestJournal)
	if err != nil {
		return empty, empty, err
	}
	result, err := r.executor.Invoke(ctx, invocation.ToolName.Name, invocation.Input)
	var rejected *toolError
	if errors.As(err, &rejected) {
		// A known rejection settles the nested intent before rejecting the host promise.
		var evidence any = map[string]any{"error": rejected.message}
		var outcome *toolOutcomeError
		if errors.As(err, &outcome) {
			evidence = outcome.evidence
		}
		if err := r.store.FinishNested(ctx, r.owner, intent, evidence); err != nil {
			return empty, empty, err
		}
		reply := codemode.DelegateResult{Status: "error", Message: rejected.message}
		return reply, reply, nil
	}
	if err != nil {
		return empty, empty, err
	}
	var evidence map[string]any
	if viewed, ok := result.(*viewImageResult); ok {
		evidence = viewed.evidence()
	} else if evidence, err = protocol.Object(result); err != nil {
		return empty, empty, err
	}
	if err := r.store.FinishNested(ctx, r.owner, intent, evidence); err != nil {
		return empty, empty, err
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return empty, empty, err
	}
	encodedEvidence, err := json.Marshal(evidence)
	if err != nil {
		return empty, empty, err
	}
	return codemode.DelegateResult{Status: "ok", Value: &codemode.ToolResult{Type: "tool/result", Result: encoded}}, codemode.DelegateResult{Status: "ok", Value: &codemode.ToolResult{Type: "tool/result", Result: encodedEvidence}}, nil
}
