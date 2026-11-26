package storagecodec

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

func Decode(data []byte, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		if err != nil {
			return err
		}
		return errors.New("trailing data after the JSON value")
	}
	return nil
}

func Required(field string, present bool) error {
	if !present {
		return fmt.Errorf("%s is required", field)
	}
	return nil
}
