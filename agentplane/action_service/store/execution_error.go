package store

import (
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"google.golang.org/genproto/googleapis/rpc/code"
	"google.golang.org/protobuf/encoding/protojson"
)

// storedErrorKinds is the `kind` each ExecutionErrorKind is stored as in `action_execution.error`. Stored rows,
// the Python service's among them, keep these spellings; UNSPECIFIED and UNKNOWN are never written.
var storedErrorKinds = map[action_servicepb.ExecutionErrorKind]string{
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INTERNAL:                    "internal",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_EXECUTION_OUTCOME_UNKNOWN:   "execution_outcome_unknown",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_COORDINATOR_STOPPED:         "coordinator_stopped",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_LEASE_EXPIRED:               "lease_expired",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_EXECUTOR_LOST:               "executor_lost",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INVALID_ARGUMENTS:           "invalid_arguments",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNKNOWN_ACTION:              "unknown_action",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INCOMPATIBLE_ACTION_SCHEMA:  "incompatible_action_schema",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_MCP_INVALID_SCHEMA:          "mcp_invalid_schema",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_MCP_UNAVAILABLE:             "mcp_unavailable",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_CALLER_NOT_LOCAL:            "caller_not_local",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_CALLER_IDENTITY_UNAVAILABLE: "caller_identity_unavailable",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_NOT_YOURS:           "sandbox_not_yours",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_UNAVAILABLE:         "sandbox_unavailable",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_NOT_FOUND:           "sandbox_not_found",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_EXISTS:              "sandbox_exists",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_REPLACED:            "sandbox_replaced",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_EXEC_FAILED:                 "exec_failed",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_BACKEND_REFUSED:             "backend_refused",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_CAPACITY_EXHAUSTED:          "capacity_exhausted",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_DISPATCHER_REFUSED:          "dispatcher_refused",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_DISPATCHER_UNREACHABLE:      "dispatcher_unreachable",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_ENVIRONMENT_REFUSED:         "environment_refused",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_ID_REUSED:                   "id_reused",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INPUT_NOT_FOUND:             "input_not_found",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_KUBERNETES_REFUSED:          "kubernetes_refused",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_LAUNCHER_GRANT_EXISTS:       "launcher_grant_exists",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_LAUNCHER_POLICY_SET:         "launcher_policy_set",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_NOT_INPUT:                   "not_input",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_READ_FAILED:                 "read_failed",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_REJECTED:                    "rejected",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SESSION_NOT_FOUND:           "session_not_found",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SOURCE_UNAVAILABLE:          "source_unavailable",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_TARGET_NOT_FOUND:            "target_not_found",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNKNOWN_POLICY_SET:          "unknown_policy_set",
	action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_WAIT_UNAVAILABLE:            "wait_unavailable",
}

// errorKindFromStored is the kind a stored `kind` names. One this release does not name, such as the Go type
// name earlier releases wrote for an unexpected failure, is UNKNOWN.
func errorKindFromStored(text string) action_servicepb.ExecutionErrorKind {
	for kind, stored := range storedErrorKinds {
		if stored == text {
			return kind
		}
	}
	return action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNKNOWN
}

// errorDocument is `action_execution.error`. A `grpc_code` marks a resume failure; an `mcp_unavailable` kind may
// carry `diagnostics`.
type errorDocument struct {
	Kind              string               `json:"kind"`
	Message           string               `json:"message"`
	Diagnostics       *diagnosticsDocument `json:"diagnostics,omitempty"`
	GRPCCode          *string              `json:"grpc_code,omitempty"`
	ResumeEligibility json.RawMessage      `json:"resume_eligibility,omitempty"`
	WakeAssessment    json.RawMessage      `json:"wake_assessment,omitempty"`
}

type diagnosticsDocument struct {
	ExceptionType    string  `json:"exception_type"`
	ConnectionAgeS   float64 `json:"connection_age_s"`
	RequestDurationS float64 `json:"request_duration_s"`
}

var storedProtoJSON = protojson.MarshalOptions{UseProtoNames: true}

func validateExecutionError(e *action_servicepb.ExecutionError) error {
	if e == nil {
		return nil
	}
	if _, stored := storedErrorKinds[e.GetKind()]; !stored {
		return invalid("an execution error names a kind it can store, not %v", e.GetKind())
	}
	if e.GetMcpUnavailable() != nil && e.GetKind() != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_MCP_UNAVAILABLE {
		return invalid("MCP diagnostics belong to an mcp_unavailable error, not %v", e.GetKind())
	}
	return nil
}

// encodeExecutionError is the stored document of a validated error; nil for none.
func encodeExecutionError(e *action_servicepb.ExecutionError) (json.RawMessage, error) {
	if e == nil {
		return nil, nil
	}
	document := errorDocument{Kind: storedErrorKinds[e.GetKind()], Message: e.GetMessage()}
	if diagnostics := e.GetMcpUnavailable().GetDiagnostics(); diagnostics != nil {
		document.Diagnostics = &diagnosticsDocument{
			ExceptionType: diagnostics.GetExceptionType(), ConnectionAgeS: diagnostics.GetConnectionAgeSeconds(),
			RequestDurationS: diagnostics.GetRequestDurationSeconds(),
		}
	}
	if resume := e.GetResume(); resume != nil {
		grpcCode := resume.GetGrpcCode().String()
		document.GRPCCode = &grpcCode
		var err error
		if resume.GetResumeEligibility() != nil {
			if document.ResumeEligibility, err = storedProtoJSON.Marshal(resume.GetResumeEligibility()); err != nil {
				return nil, err
			}
		}
		if resume.GetWakeAssessment() != nil {
			if document.WakeAssessment, err = storedProtoJSON.Marshal(resume.GetWakeAssessment()); err != nil {
				return nil, err
			}
		}
	}
	return json.Marshal(document)
}

// decodeExecutionError reads a stored document; nil for none.
func decodeExecutionError(stored json.RawMessage) (*action_servicepb.ExecutionError, error) {
	if stored == nil {
		return nil, nil
	}
	var document errorDocument
	if err := json.Unmarshal(stored, &document); err != nil {
		return nil, fmt.Errorf("stored execution error: %w", err)
	}
	decoded := &action_servicepb.ExecutionError{Kind: errorKindFromStored(document.Kind), Message: document.Message}
	switch {
	case document.GRPCCode != nil:
		grpcCode, known := code.Code_value[*document.GRPCCode]
		if !known {
			grpcCode = int32(code.Code_UNKNOWN)
		}
		resume := &action_servicepb.ResumeFailure{GrpcCode: code.Code(grpcCode)}
		if document.ResumeEligibility != nil {
			resume.ResumeEligibility = &environmentlifecyclepb.ResumeEligibility{}
			if err := protojson.Unmarshal(document.ResumeEligibility, resume.ResumeEligibility); err != nil {
				return nil, fmt.Errorf("stored execution error resume_eligibility: %w", err)
			}
		}
		if document.WakeAssessment != nil {
			resume.WakeAssessment = &environmentlifecyclepb.WakeAssessment{}
			if err := protojson.Unmarshal(document.WakeAssessment, resume.WakeAssessment); err != nil {
				return nil, fmt.Errorf("stored execution error wake_assessment: %w", err)
			}
		}
		decoded.Detail = &action_servicepb.ExecutionError_Resume{Resume: resume}
	case decoded.Kind == action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_MCP_UNAVAILABLE:
		unavailable := &action_servicepb.McpUnavailable{}
		if diagnostics := document.Diagnostics; diagnostics != nil {
			unavailable.Diagnostics = &action_servicepb.McpRequestDiagnostics{
				ExceptionType: diagnostics.ExceptionType, ConnectionAgeSeconds: diagnostics.ConnectionAgeS,
				RequestDurationSeconds: diagnostics.RequestDurationS,
			}
		}
		decoded.Detail = &action_servicepb.ExecutionError_McpUnavailable{McpUnavailable: unavailable}
	}
	return decoded, nil
}
