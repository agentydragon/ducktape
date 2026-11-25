package sessionstore

import (
	"cmp"
	"encoding/json"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

type modelExecutionSnapshot struct {
	Cells     []modelCell    `json:"cells"`
	Processes []modelProcess `json:"processes"`
}
type (
	modelCell struct {
		CellID string                    `json:"cell_id"`
		Status executionstate.CellStatus `json:"status"`
	}
	modelProcess struct {
		SessionID int                          `json:"session_id"`
		Status    executionstate.ProcessStatus `json:"status"`
		TTY       bool                         `json:"tty"`
		ExitCode  *int                         `json:"exit_code"`
	}
)

// appendExecutionSnapshot records the handles at compaction commit in the replacement
// context. An empty snapshot is meaningful here: a summary can mention retired handles.
func (s *Store) appendExecutionSnapshot(objects content.ContentWriter, conversation *Conversation, native protocol.Context, state *ExecutionState) error {
	snapshot := modelExecutionSnapshot{Cells: []modelCell{}, Processes: []modelProcess{}}
	for _, cell := range state.Cells {
		snapshot.Cells = append(snapshot.Cells, modelCell{cell.ID, cell.Status})
	}
	for _, process := range state.Processes {
		if process.ModelSessionID != nil {
			processSnapshot := modelProcess{SessionID: *process.ModelSessionID, Status: process.Status, TTY: process.TTY}
			if process.ExitCode != nil {
				processSnapshot.ExitCode = new(*process.ExitCode)
			}
			snapshot.Processes = append(snapshot.Processes, processSnapshot)
		}
	}
	slices.SortFunc(snapshot.Cells, func(a, b modelCell) int { return strings.Compare(a.CellID, b.CellID) })
	slices.SortFunc(snapshot.Processes, func(a, b modelProcess) int { return cmp.Compare(a.SessionID, b.SessionID) })
	var origin protocol.RecordOrigin
	if s.contextSourceWrites {
		sourceID, err := putRecordObject(objects, ExecutionSnapshotObject{
			ConversationID: conversation.ID, modelExecutionSnapshot: snapshot,
		})
		if err != nil {
			return err
		}
		origin = protocol.RecordOrigin{Kind: protocol.RecordOriginExecutionSnapshot, ObjectID: sourceID}
	}
	raw, err := json.Marshal(snapshot)
	if err != nil {
		return err
	}
	if err := native.AppendNotice([]protocol.ContentPart{{Type: "input_text", Text: "Active execution handles: " + string(raw)}}, origin); err != nil {
		return err
	}
	// Preserve the control format for historical request replay and for an older
	// sampler that resumes this context after rollback.
	conversation.executionSnapshots[native] = &snapshot
	return nil
}
