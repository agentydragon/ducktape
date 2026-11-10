package main

import (
	"encoding/json"
	"os"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// A list is CLI framing; each element uses the upstream protobuf JSON mapping.
func printProtoJSONArray(messages []proto.Message) error {
	items := make([]json.RawMessage, 0, len(messages))
	for _, message := range messages {
		item, err := protojson.Marshal(message)
		if err != nil {
			return err
		}
		items = append(items, item)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(items)
}
