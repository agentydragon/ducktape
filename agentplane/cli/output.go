package main

import (
	"bytes"
	"encoding/json"
	"strings"
)

func decode[T any](raw []byte) (T, error) {
	var value T
	if err := json.Unmarshal(raw, &value); err != nil {
		return value, invalidResponse("%v", err)
	}
	return value, nil
}

// show prints items: under --json each one's JSON, compacted but otherwise unmodified, one per line;
// otherwise the text line of each.
func (a *app) show(items []json.RawMessage, line func(json.RawMessage) (string, error)) error {
	var out bytes.Buffer
	for _, item := range items {
		if a.asJSON {
			if err := json.Compact(&out, item); err != nil {
				return invalidResponse("%v", err)
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
	if _, err := a.stdout.Write(out.Bytes()); err != nil {
		return clientFailure(exitRefused, "INTERNAL", "write stdout: %v", err)
	}
	return nil
}

// showOne is show for a command that answers one value.
func (a *app) showOne(raw []byte, line func(json.RawMessage) (string, error)) error {
	return a.show([]json.RawMessage{raw}, line)
}
