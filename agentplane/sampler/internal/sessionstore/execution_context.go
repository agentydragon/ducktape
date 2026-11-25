package sessionstore

import (
	"cmp"
	"encoding/json"
	"reflect"
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

func (s *Store) appendExecutionState(objects content.ContentWriter, conversation *Conversation, state *protocol.ExecutionState) error {
	if state == nil {
		return nil
	}
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
	if conversation.executionSnapshots == nil {
		conversation.executionSnapshots = map[protocol.Context]*modelExecutionSnapshot{}
	}
	var sourceID, notice string
	return conversation.apply(func(native protocol.Context) error {
		previous := conversation.executionSnapshots[native]
		// A missing baseline is unknown, including after migration or compaction.
		if reflect.DeepEqual(previous, &snapshot) {
			return nil
		}
		if notice == "" {
			if s.contextSourceWrites {
				var err error
				sourceID, err = putRecordObject(objects, ExecutionSnapshotObject{
					ConversationID: conversation.ID, modelExecutionSnapshot: snapshot,
				})
				if err != nil {
					return err
				}
			}
			raw, err := json.Marshal(snapshot)
			if err != nil {
				return err
			}
			notice = "Active execution handles: " + string(raw)
		}
		var origin protocol.RecordOrigin
		if sourceID != "" {
			origin = protocol.RecordOrigin{Kind: protocol.RecordOriginExecutionSnapshot, ObjectID: sourceID}
		}
		if err := native.AppendNotice([]protocol.ContentPart{{Type: "input_text", Text: notice}}, origin); err != nil {
			return err
		}
		conversation.executionSnapshots[native] = &snapshot
		return nil
	})
}
