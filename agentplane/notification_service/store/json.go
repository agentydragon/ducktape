package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
)

func decode(data []byte, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if decoder.More() {
		return errors.New("trailing data after the JSON value")
	}
	return nil
}

func required(field string, present bool) error {
	if !present {
		return fmt.Errorf("%s is required", field)
	}
	return nil
}
