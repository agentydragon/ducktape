package invocation

import (
	"encoding/json"

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

func ProtoJSONList[T proto.Message](messages []T) ([]json.RawMessage, error) {
	return ProtoJSONListWithOptions(messages, protojson.MarshalOptions{})
}

func ProtoJSONListWithOptions[T proto.Message](messages []T, options protojson.MarshalOptions) ([]json.RawMessage, error) {
	items := make([]json.RawMessage, 0, len(messages))
	for _, message := range messages {
		encoded, err := ProtoJSONWithOptions(message, options)
		if err != nil {
			return nil, err
		}
		items = append(items, encoded)
	}
	return items, nil
}
