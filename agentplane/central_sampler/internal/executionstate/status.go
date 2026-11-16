package executionstate

import (
	"fmt"
)

// ProcessStatus is the lifecycle state of a sampler-tracked remote process.
type ProcessStatus string

const (
	ProcessStarting ProcessStatus = "starting"
	// ProcessRunning retains an open output handle; a stored exit code means the
	// process has exited but its output has not yet closed.
	ProcessRunning ProcessStatus = "running"
	// ProcessExited has been fully consumed; ProcessExitedUnread still holds unread output.
	ProcessExited       ProcessStatus = "exited"
	ProcessExitedUnread ProcessStatus = "exited_unread"
	ProcessAbandoned    ProcessStatus = "abandoned"
	// ProcessUnknown is a process whose remote outcome could not be determined.
	ProcessUnknown ProcessStatus = "unknown"
)

func (status ProcessStatus) Valid() bool {
	switch status {
	case ProcessStarting, ProcessRunning, ProcessExited, ProcessExitedUnread, ProcessAbandoned, ProcessUnknown:
		return true
	default:
		return false
	}
}

// ParseProcessStatus rejects values outside the stored lifecycle vocabulary.
func ParseProcessStatus(value string) (ProcessStatus, error) {
	status := ProcessStatus(value)
	if !status.Valid() {
		return "", fmt.Errorf("unknown process status %q", value)
	}
	return status, nil
}

// CellStatus is the lifecycle state of a live code cell.
type CellStatus string

const (
	CellLive       CellStatus = "live"
	CellCompleted  CellStatus = "completed"
	CellTerminated CellStatus = "terminated"
	CellAbandoned  CellStatus = "abandoned"
)

func (status CellStatus) Valid() bool {
	switch status {
	case CellLive, CellCompleted, CellTerminated, CellAbandoned:
		return true
	default:
		return false
	}
}

// ParseCellStatus rejects values outside the stored lifecycle vocabulary.
func ParseCellStatus(value string) (CellStatus, error) {
	status := CellStatus(value)
	if !status.Valid() {
		return "", fmt.Errorf("unknown cell status %q", value)
	}
	return status, nil
}
