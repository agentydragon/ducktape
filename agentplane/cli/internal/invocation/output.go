package invocation

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// show prints items: under --json each one's JSON, compacted but otherwise unmodified, one per line;
// otherwise the text line of each.
func (a *Invocation) Show(items []json.RawMessage, line func(json.RawMessage) (string, error)) error {
	var out bytes.Buffer
	for _, item := range items {
		if a.asJSON {
			if err := json.Compact(&out, item); err != nil {
				return InvalidResponse("%v", err)
			}
		} else {
			text, err := line(item)
			if err != nil {
				return err
			}
			out.WriteString(strings.TrimRight(text, "\n"))
		}
		out.WriteByte('\n')
	}
	return a.write(&out)
}

func ShowProto[T proto.Message](a *Invocation, messages []T, line func(T) (string, error), options protojson.MarshalOptions) error {
	if a.asJSON {
		items, err := ProtoJSONListWithOptions(messages, options)
		if err != nil {
			return err
		}
		return a.Show(items, nil)
	}
	var out bytes.Buffer
	for _, message := range messages {
		text, err := line(message)
		if err != nil {
			return err
		}
		out.WriteString(strings.TrimRight(text, "\n"))
		out.WriteByte('\n')
	}
	return a.write(&out)
}

func (a *Invocation) write(out *bytes.Buffer) error {
	if _, err := a.stdout.Write(out.Bytes()); err != nil {
		return ClientFailure(ExitRefused, "INTERNAL", "write stdout: %v", err)
	}
	return nil
}

// showOne is show for a command that answers one value.
func (a *Invocation) ShowOne(raw []byte, line func(json.RawMessage) (string, error)) error {
	return a.Show([]json.RawMessage{raw}, line)
}

func (a *Invocation) AnnounceKey(key string) {
	if a.JSON() {
		fmt.Fprintf(a.Stderr(), "{\"idempotency_key\": %q}\n", key)
	} else {
		fmt.Fprintf(a.Stderr(), "idempotency key: %s\n", key)
	}
}
