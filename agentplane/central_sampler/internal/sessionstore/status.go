package sessionstore

import (
	"database/sql/driver"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/executionstate"
)

type processStatus executionstate.ProcessStatus

func (status *processStatus) Scan(src any) error {
	var value string
	switch typed := src.(type) {
	case string:
		value = typed
	case []byte:
		value = string(typed)
	default:
		return fmt.Errorf("process status has type %T, expected string", src)
	}
	candidate, err := executionstate.ParseProcessStatus(value)
	if err != nil {
		return err
	}
	*status = processStatus(candidate)
	return nil
}

func (status processStatus) Value() (driver.Value, error) {
	value, err := executionstate.ParseProcessStatus(string(status))
	if err != nil {
		return nil, err
	}
	return string(value), nil
}

type cellStatus executionstate.CellStatus

func (status *cellStatus) Scan(src any) error {
	var value string
	switch typed := src.(type) {
	case string:
		value = typed
	case []byte:
		value = string(typed)
	default:
		return fmt.Errorf("cell status has type %T, expected string", src)
	}
	candidate, err := executionstate.ParseCellStatus(value)
	if err != nil {
		return err
	}
	*status = cellStatus(candidate)
	return nil
}

func (status cellStatus) Value() (driver.Value, error) {
	value, err := executionstate.ParseCellStatus(string(status))
	if err != nil {
		return nil, err
	}
	return string(value), nil
}
