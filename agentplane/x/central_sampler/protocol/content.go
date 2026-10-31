package protocol

import (
	"bytes"
	"encoding/json"
	"errors"
)

type ContentPart struct {
	Type string          `json:"type"`
	Text string          `json:"text"`
	Raw  json.RawMessage `json:"-"`
}

func (part *ContentPart) UnmarshalJSON(data []byte) error {
	if !bytes.HasPrefix(bytes.TrimSpace(data), []byte("{")) {
		return errors.New("content part must be an object")
	}
	var wire struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	*part = ContentPart{Type: wire.Type, Text: wire.Text, Raw: CloneJSON(data)}
	return nil
}

func (part ContentPart) MarshalJSON() ([]byte, error) {
	if len(part.Raw) != 0 {
		return CloneJSON(part.Raw), nil
	}
	type wire ContentPart
	return json.Marshal(wire(part))
}
