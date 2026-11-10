package protocol

import (
	"database/sql/driver"
	"fmt"
)

// ProcessStatus is the lifecycle state of a sampler-tracked remote process.
type ProcessStatus string

const (
	ProcessStarting ProcessStatus = "starting"
	ProcessRunning  ProcessStatus = "running"
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

// Scan rejects stored statuses the sampler does not know.
func (status *ProcessStatus) Scan(src any) error {
	var value string
	switch typed := src.(type) {
	case string:
		value = typed
	case []byte:
		value = string(typed)
	default:
		return fmt.Errorf("process status has type %T, expected string", src)
	}
	candidate := ProcessStatus(value)
	if !candidate.Valid() {
		return fmt.Errorf("unknown process status %q", value)
	}
	*status = candidate
	return nil
}

// Value rejects statuses the sampler does not know, so an unknown one never reaches the database.
func (status ProcessStatus) Value() (driver.Value, error) {
	if !status.Valid() {
		return nil, fmt.Errorf("unknown process status %q", string(status))
	}
	return string(status), nil
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

// Scan rejects stored statuses the sampler does not know.
func (status *CellStatus) Scan(src any) error {
	var value string
	switch typed := src.(type) {
	case string:
		value = typed
	case []byte:
		value = string(typed)
	default:
		return fmt.Errorf("cell status has type %T, expected string", src)
	}
	candidate := CellStatus(value)
	if !candidate.Valid() {
		return fmt.Errorf("unknown cell status %q", value)
	}
	*status = candidate
	return nil
}

// Value rejects statuses the sampler does not know, so an unknown one never reaches the database.
func (status CellStatus) Value() (driver.Value, error) {
	if !status.Valid() {
		return nil, fmt.Errorf("unknown cell status %q", string(status))
	}
	return string(status), nil
}
