package main

import (
	"errors"
	"fmt"
)

// rawCodec lets grpc-go relay each message payload without knowing the service's protobuf schema.
type rawCodec struct{}

func (rawCodec) Marshal(value any) ([]byte, error) {
	switch message := value.(type) {
	case []byte:
		return message, nil
	case *[]byte:
		return *message, nil
	default:
		return nil, fmt.Errorf("raw gRPC codec cannot marshal %T", value)
	}
}

func (rawCodec) Unmarshal(data []byte, value any) error {
	message, okay := value.(*[]byte)
	if !okay {
		return fmt.Errorf("raw gRPC codec cannot unmarshal into %T", value)
	}
	if message == nil {
		return errors.New("raw gRPC codec received a nil message pointer")
	}
	*message = append((*message)[:0], data...)
	return nil
}

func (rawCodec) Name() string { return "proto" }
