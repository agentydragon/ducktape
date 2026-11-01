// Package jsonrpc is the JSON-RPC 2.0 dialect Codex's app-server and exec-server speak: messages
// omit the `jsonrpc` member. Callers marshal and read whole frames themselves, so the bytes they
// journal are the bytes on the wire.
package jsonrpc

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
)

// Request is a request, or a notification when ID is empty. Params is omitted only when it is a nil
// interface, pointer, map or slice; a struct value is always written, so `"params":{}` stays.
type Request[P any] struct {
	ID     string `json:"id,omitempty"`
	Method string `json:"method"`
	Params P      `json:"params,omitempty"`
}

// ErrorReply refuses a request the peer sent.
type ErrorReply struct {
	Error Error           `json:"error"`
	ID    json.RawMessage `json:"id"`
}

type Error struct {
	Code    int64           `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

// Header is the part of a received frame that routes it.
type Header struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
}

// Message is any received frame. Params and Result stay opaque until the caller's method or pending
// request selects a type.
type Message struct {
	Header
	Params json.RawMessage `json:"params"`
	Result json.RawMessage `json:"result"`
	Error  *Error          `json:"error"`
}

// Parse decodes one frame.
func Parse(frame []byte) (Message, error) {
	var message Message
	err := UnmarshalObject(frame, &message)
	return message, err
}

// HasID reports whether the frame carries a non-null ID. A frame without one is a notification.
func (h Header) HasID() bool {
	return len(h.ID) != 0 && !bytes.Equal(bytes.TrimSpace(h.ID), []byte("null"))
}

// IsRequest reports whether the peer expects a reply to this frame.
func (h Header) IsRequest() bool { return h.HasID() && h.Method != "" }

// IsResponse reports whether the frame answers a request.
func (h Header) IsResponse() bool { return h.HasID() && h.Method == "" }

// StringID returns the ID when it is a JSON string; every request this runner sends has one.
func (h Header) StringID() (string, bool) {
	var id string
	return id, json.Unmarshal(h.ID, &id) == nil
}

// Answers reports whether the frame is the response to the request named `id`.
func (h Header) Answers(id string) bool {
	got, ok := h.StringID()
	return h.IsResponse() && ok && got == id
}

// UnmarshalObject decodes exactly one JSON object, keeping numbers in `any` fields as json.Number.
func UnmarshalObject(data []byte, value any) error {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return errors.New("protocol payload must be a JSON object")
	}
	decoder := json.NewDecoder(bytes.NewReader(trimmed))
	decoder.UseNumber()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("protocol payload has trailing JSON")
	}
	return nil
}
