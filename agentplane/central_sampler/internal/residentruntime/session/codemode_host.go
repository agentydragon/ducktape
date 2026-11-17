package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/actions"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/executor"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/jsonwire"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

// CodeModeHost binds sampler ownership and tool execution to the shared host.
type CodeModeHost struct {
	*codemode.Manager
	store   *sessionstore.Store
	actions *actions.Client
}

func NewCodeModeHost(manager *codemode.Manager, store *sessionstore.Store, actionClient *actions.Client) *CodeModeHost {
	return &CodeModeHost{Manager: manager, store: store, actions: actionClient}
}

func (m *CodeModeHost) OpenSession(ctx context.Context, conversationID string, owner sessionstore.Owner, executor *executor.Executor) (*CodeMode, error) {
	code := &CodeMode{store: m.store, owner: owner, executor: executor}
	if m.actions != nil {
		retained, err := m.store.GetSessionForConversation(ctx, owner)
		if err != nil {
			return nil, err
		}
		identity, err := retained.ExecutionIdentity()
		if err != nil {
			return nil, err
		}
		code.actions, err = m.actions.Bind(retained.ID, identity)
		if err != nil {
			return nil, err
		}
	}
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
	store *sessionstore.Store
	owner sessionstore.Owner
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
	return j.store.CellResult(ctx, j.owner, cellID, executionstate.CellLive)
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
	if invocation.ToolName.Name == "" {
		return empty, empty, errors.New("code-mode tool invocation has no supported tool name")
	}
	requestJournal, err := jsonwire.Object(invocation)
	if err != nil {
		return empty, empty, err
	}
	if invocation.CellID != "" {
		requestJournal["cell_id"] = session.CellHandle(invocation.CellID)
	}
	intent, err := r.store.RecordNested(ctx, r.owner, sessionstore.IntentTool, requestJournal)
	if err != nil {
		return empty, empty, err
	}
	if invocation.ToolName.Namespace != nil {
		return r.invokeAction(ctx, intent, invocation)
	}
	if r.executor == nil {
		reply := codemode.DelegateResult{Status: "error", Message: "this tool requires an attached environment"}
		if err := r.store.FinishNested(ctx, r.owner, intent, map[string]any{"error": reply.Message}); err != nil {
			return empty, empty, err
		}
		return reply, reply, nil
	}
	result, err := r.executor.Invoke(ctx, invocation.ToolName.Name, invocation.Input)
	var rejected *toolcontract.Rejection
	if errors.As(err, &rejected) {
		// A known rejection settles the nested intent before rejecting the host promise.
		var evidence any = map[string]any{"error": rejected.Message}
		var outcome *executor.ToolOutcomeError
		if errors.As(err, &outcome) {
			evidence = outcome.Evidence()
		}
		if err := r.store.FinishNested(ctx, r.owner, intent, evidence); err != nil {
			return empty, empty, err
		}
		reply := codemode.DelegateResult{Status: "error", Message: rejected.Message}
		return reply, reply, nil
	}
	if err != nil {
		return empty, empty, err
	}
	var evidence map[string]any
	if viewed, ok := result.(*executor.ViewImageResult); ok {
		evidence = viewed.Evidence()
	} else if evidence, err = jsonwire.Object(result); err != nil {
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
