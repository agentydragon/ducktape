package centralsampler

import (
	"cmp"
	"encoding/json"
	"reflect"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

type modelExecutionSnapshot struct {
	Cells     []modelCell    `json:"cells"`
	Processes []modelProcess `json:"processes"`
}
type (
	modelCell struct {
		CellID string              `json:"cell_id"`
		Status protocol.CellStatus `json:"status"`
	}
	modelProcess struct {
		SessionID int                    `json:"session_id"`
		Status    protocol.ProcessStatus `json:"status"`
		TTY       bool                   `json:"tty"`
		ExitCode  *int                   `json:"exit_code"`
	}
)

func (conversation *Conversation) appendExecutionState(state *protocol.ExecutionState) error {
	if state == nil {
		return nil
	}
	snapshot := modelExecutionSnapshot{Cells: []modelCell{}, Processes: []modelProcess{}}
	for _, cell := range state.Cells {
		snapshot.Cells = append(snapshot.Cells, modelCell{cell.ID, cell.Status})
	}
	for _, process := range state.Processes {
		if process.ModelSessionID != nil {
			snapshot.Processes = append(snapshot.Processes, modelProcess{*process.ModelSessionID, process.Status, process.TTY, process.ExitCode})
		}
	}
	slices.SortFunc(snapshot.Cells, func(a, b modelCell) int { return strings.Compare(a.CellID, b.CellID) })
	slices.SortFunc(snapshot.Processes, func(a, b modelProcess) int { return cmp.Compare(a.SessionID, b.SessionID) })
	if conversation.executionSnapshots == nil {
		conversation.executionSnapshots = map[protocol.Context]*modelExecutionSnapshot{}
	}
	return conversation.apply(func(native protocol.Context) error {
		previous := conversation.executionSnapshots[native]
		// A missing baseline is unknown, including after migration or compaction.
		if reflect.DeepEqual(previous, &snapshot) {
			return nil
		}
		raw, err := json.Marshal(snapshot)
		if err != nil {
			return err
		}
		if err := native.AppendNotice([]protocol.ContentPart{{Type: "input_text", Text: "Active execution handles: " + string(raw)}}); err != nil {
			return err
		}
		conversation.executionSnapshots[native] = &snapshot
		return nil
	})
}
