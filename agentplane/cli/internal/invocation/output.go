package invocation

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// ShowProto buffers the whole result so an invalid item cannot leave a partial page on stdout.
func ShowProto[T proto.Message](a *Invocation, messages []T, line func(T) (string, error), options protojson.MarshalOptions) error {
	var out bytes.Buffer
	for _, message := range messages {
		if a.asJSON {
			raw, err := ProtoJSONWithOptions(message, options)
			if err != nil {
				return err
			}
			if err := json.Compact(&out, raw); err != nil {
				return InvalidResponse("%v", err)
			}
		} else {
			text, err := line(message)
			if err != nil {
				return err
			}
			out.WriteString(strings.TrimRight(text, "\n"))
		}
		out.WriteByte('\n')
	}
	return a.write(out.Bytes())
}

// ShowValue writes a non-protobuf value as one JSON line or its supplied text verbatim.
func (a *Invocation) ShowValue(value any, text string) error {
	if !a.asJSON {
		return a.write([]byte(text))
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return InvalidResponse("%v", err)
	}
	return a.write(append(raw, '\n'))
}

func (a *Invocation) write(data []byte) error {
	if _, err := a.stdout.Write(data); err != nil {
		return ClientFailure(ExitRefused, "INTERNAL", "write stdout: %v", err)
	}
	return nil
}

func (a *Invocation) AnnounceKey(key string) {
	if a.JSON() {
		fmt.Fprintf(a.Stderr(), "{\"idempotency_key\": %q}\n", key)
	} else {
		fmt.Fprintf(a.Stderr(), "idempotency key: %s\n", key)
	}
}
