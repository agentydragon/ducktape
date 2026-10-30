package jsonrpc

import (
	"encoding/json"
	"testing"
)

func TestRequestOmitsOnlyAbsentIDAndParams(t *testing.T) {
	for _, test := range []struct {
		name  string
		value any
		want  string
	}{
		{"request", Request[any]{ID: "r", Method: "m", Params: struct{}{}}, `{"id":"r","method":"m","params":{}}`},
		{"notification without params", Request[any]{Method: "initialized"}, `{"method":"initialized"}`},
		{"notification with empty params", Request[struct{}]{Method: "initialized"}, `{"method":"initialized","params":{}}`},
		{"error reply", ErrorReply{ID: json.RawMessage(`7`), Error: Error{Code: -32601, Message: "no"}}, `{"error":{"code":-32601,"message":"no"},"id":7}`},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := json.Marshal(test.value)
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != test.want {
				t.Fatalf("encoded %s, want %s", got, test.want)
			}
		})
	}
}

func TestMessageClassification(t *testing.T) {
	for _, test := range []struct {
		frame                string
		request, response    bool
		answersRequestNamedR bool
	}{
		{`{"id":"r","result":{}}`, false, true, true},
		{`{"id":"r","error":{"code":1,"message":"no"}}`, false, true, true},
		{`{"id":7,"result":{}}`, false, true, false},
		{`{"id":"r","method":"m","params":{}}`, true, false, false},
		{`{"id":null,"method":"m"}`, false, false, false},
		{`{"method":"m"}`, false, false, false},
	} {
		t.Run(test.frame, func(t *testing.T) {
			message, err := Parse([]byte(test.frame))
			if err != nil {
				t.Fatal(err)
			}
			if message.IsRequest() != test.request || message.IsResponse() != test.response || message.Answers("r") != test.answersRequestNamedR {
				t.Fatalf("request=%v response=%v answers=%v", message.IsRequest(), message.IsResponse(), message.Answers("r"))
			}
		})
	}
}

func TestParseRejectsAllButOneObject(t *testing.T) {
	for _, frame := range []string{``, `null`, `[]`, `{"id":"r"}{"id":"s"}`} {
		if _, err := Parse([]byte(frame)); err == nil {
			t.Fatalf("Parse(%q) accepted", frame)
		}
	}
}
