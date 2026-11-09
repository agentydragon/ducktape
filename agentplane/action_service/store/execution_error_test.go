package store

import (
	"encoding/json"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"google.golang.org/genproto/googleapis/rpc/code"
	"google.golang.org/protobuf/proto"
)

// Documents in the shapes earlier releases stored read as typed errors, and typed errors store those shapes.
func TestStoredExecutionErrorsKeepTheirShape(t *testing.T) {
	for name, test := range map[string]struct {
		stored string
		typed  *action_servicepb.ExecutionError
	}{
		"a refusal": {
			`{"kind": "sandbox_not_found", "message": "test message"}`,
			&action_servicepb.ExecutionError{Kind: action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_NOT_FOUND, Message: "test message"},
		},
		"a resume failure": {
			`{"kind": "rejected", "message": "test message", "grpc_code": "FAILED_PRECONDITION",
				"resume_eligibility": {"state": "RESUME_ELIGIBILITY_STATE_DENIED"}, "wake_assessment": {"state": "WAKE_STATE_RATE_LIMITED"}}`,
			&action_servicepb.ExecutionError{
				Kind: action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_REJECTED, Message: "test message",
				Detail: &action_servicepb.ExecutionError_Resume{Resume: &action_servicepb.ResumeFailure{
					GrpcCode:          code.Code_FAILED_PRECONDITION,
					ResumeEligibility: &environmentlifecyclepb.ResumeEligibility{State: environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED},
					WakeAssessment:    &environmentlifecyclepb.WakeAssessment{State: environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED},
				}},
			},
		},
		"a resume failure without details": {
			`{"kind": "execution_outcome_unknown", "message": "test message", "grpc_code": "UNAVAILABLE"}`,
			&action_servicepb.ExecutionError{
				Kind: action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_EXECUTION_OUTCOME_UNKNOWN, Message: "test message",
				Detail: &action_servicepb.ExecutionError_Resume{Resume: &action_servicepb.ResumeFailure{GrpcCode: code.Code_UNAVAILABLE}},
			},
		},
		"an MCP group with diagnostics": {
			`{"kind": "mcp_unavailable", "message": "test message",
				"diagnostics": {"connection_age_s": 1.5, "request_duration_s": 0.25}}`,
			&action_servicepb.ExecutionError{
				Kind: action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_MCP_UNAVAILABLE, Message: "test message",
				Detail: &action_servicepb.ExecutionError_McpUnavailable{McpUnavailable: &action_servicepb.McpUnavailable{
					Diagnostics: &action_servicepb.McpRequestDiagnostics{ConnectionAgeSeconds: 1.5, RequestDurationSeconds: 0.25},
				}},
			},
		},
		"an MCP group without diagnostics": {
			`{"kind": "mcp_unavailable", "message": "test message"}`,
			&action_servicepb.ExecutionError{
				Kind: action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_MCP_UNAVAILABLE, Message: "test message",
				Detail: &action_servicepb.ExecutionError_McpUnavailable{McpUnavailable: &action_servicepb.McpUnavailable{}},
			},
		},
	} {
		t.Run(name, func(t *testing.T) {
			decoded, err := decodeExecutionError(json.RawMessage(test.stored))
			if err != nil || !proto.Equal(decoded, test.typed) {
				t.Fatalf("decoded %v, %v; want %v", decoded, err, test.typed)
			}
			if err := validateExecutionError(test.typed); err != nil {
				t.Fatal(err)
			}
			encoded, err := encodeExecutionError(test.typed)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, encoded, []byte(test.stored))
		})
	}
}

// Rows from earlier releases name the Go type of the MCP request's failure; it reads as absent.
func TestStoredMcpExceptionTypesAreDropped(t *testing.T) {
	decoded, err := decodeExecutionError(json.RawMessage(`{"kind": "mcp_unavailable", "message": "test message",
		"diagnostics": {"exception_type": "*test.Error", "connection_age_s": 1.5, "request_duration_s": 0.25}}`))
	want := &action_servicepb.ExecutionError{
		Kind: action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_MCP_UNAVAILABLE, Message: "test message",
		Detail: &action_servicepb.ExecutionError_McpUnavailable{McpUnavailable: &action_servicepb.McpUnavailable{
			Diagnostics: &action_servicepb.McpRequestDiagnostics{ConnectionAgeSeconds: 1.5, RequestDurationSeconds: 0.25},
		}},
	}
	if err != nil || !proto.Equal(decoded, want) {
		t.Errorf("decoded %v, %v; want %v", decoded, err, want)
	}
}

// A kind this release does not name, such as the Go type name earlier releases stored for an unexpected failure,
// reads as UNKNOWN with its message; UNKNOWN is never written.
func TestUnnamedStoredKindsReadAsUnknown(t *testing.T) {
	for _, stored := range []string{`{"kind": "*fmt.wrapError", "message": "test message"}`, `{"message": "test message"}`} {
		decoded, err := decodeExecutionError(json.RawMessage(stored))
		if err != nil || decoded.GetKind() != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNKNOWN || decoded.GetMessage() != "test message" {
			t.Errorf("%s decoded %v, %v", stored, decoded, err)
		}
		if err := validateExecutionError(decoded); !errors.Is(err, ErrInvalid) {
			t.Errorf("writing %v = %v, want ErrInvalid", decoded, err)
		}
	}
}

// Every kind a writer can name has one stored spelling of its own.
func TestEveryWritableKindIsStored(t *testing.T) {
	kinds := map[string]action_servicepb.ExecutionErrorKind{}
	for number := range action_servicepb.ExecutionErrorKind_name {
		kind := action_servicepb.ExecutionErrorKind(number)
		if kind == action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNSPECIFIED || kind == action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNKNOWN {
			continue
		}
		stored, ok := storedErrorKinds[kind]
		if !ok {
			t.Errorf("%v has no stored kind", kind)
		}
		if other, taken := kinds[stored]; taken {
			t.Errorf("%v and %v are both stored as %q", kind, other, stored)
		}
		kinds[stored] = kind
	}
}
