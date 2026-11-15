package invocation

import (
	"encoding/json"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

func ProtoJSON(message proto.Message) ([]byte, error) {
	encoded, err := protojson.Marshal(message)
	if err != nil {
		return nil, InvalidResponse("%v", err)
	}
	return encoded, nil
}

func ProtoJSONList[T proto.Message](messages []T) ([]json.RawMessage, error) {
	items := make([]json.RawMessage, 0, len(messages))
	for _, message := range messages {
		encoded, err := ProtoJSON(message)
		if err != nil {
			return nil, err
		}
		items = append(items, encoded)
	}
	return items, nil
}
