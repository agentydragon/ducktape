package model

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

// ServiceAccount is comparable so ownership checks use namespace/name values, not protobuf pointer identity.
type ServiceAccount struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

// DestinationRef is the Sandbox a Session runs in, by the UID that tells it from a Sandbox recreated under
// the same name. It is stored as `inbox.destination_ref` and inside a subscription's creation.
type DestinationRef struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	UID       string `json:"uid"`
}

// DestinationKey is the identity of an owner's inbox for a Session: the SHA-256 of the JSON that Python's
// `json.dumps([destination_ref, session_id], sort_keys=True)` writes, in lowercase hexadecimal. Existing
// inboxes are found by it, so any other serialization makes each of them look new.
func DestinationKey(ref DestinationRef, sessionID string) string {
	var serialized strings.Builder
	serialized.WriteString(`[{"name": `)
	writePythonString(&serialized, ref.Name)
	serialized.WriteString(`, "namespace": `)
	writePythonString(&serialized, ref.Namespace)
	serialized.WriteString(`, "uid": `)
	writePythonString(&serialized, ref.UID)
	serialized.WriteString(`}, `)
	writePythonString(&serialized, sessionID)
	serialized.WriteString(`]`)
	sum := sha256.Sum256([]byte(serialized.String()))
	return hex.EncodeToString(sum[:])
}

// writePythonString writes s as `json.dumps` does with its default `ensure_ascii`: everything outside space to
// tilde is escaped, in lowercase hexadecimal, with surrogate pairs above the basic plane.
func writePythonString(out *strings.Builder, s string) {
	out.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			out.WriteString(`\"`)
		case r == '\\':
			out.WriteString(`\\`)
		case r == '\n':
			out.WriteString(`\n`)
		case r == '\r':
			out.WriteString(`\r`)
		case r == '\t':
			out.WriteString(`\t`)
		case r == '\b':
			out.WriteString(`\b`)
		case r == '\f':
			out.WriteString(`\f`)
		case r >= ' ' && r <= '~':
			out.WriteRune(r)
		case r > 0xFFFF:
			r -= 0x10000
			fmt.Fprintf(out, `\u%04x\u%04x`, 0xD800+(r>>10), 0xDC00+(r&0x3FF))
		default:
			fmt.Fprintf(out, `\u%04x`, r)
		}
	}
	out.WriteByte('"')
}
