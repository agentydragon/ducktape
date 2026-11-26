package invocation

import (
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

func ProtoJSON(message proto.Message) ([]byte, error) {
	return ProtoJSONWithOptions(message, protojson.MarshalOptions{})
}

func ProtoJSONWithOptions(message proto.Message, options protojson.MarshalOptions) ([]byte, error) {
	encoded, err := options.Marshal(message)
	if err != nil {
		return nil, InvalidResponse("%v", err)
	}
	return encoded, nil
}
