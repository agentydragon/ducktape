// Package outputbuffer retains bounded process output as bytes.
package outputbuffer

import "fmt"

// MaxBytes is the unified exec output bound used by the central sampler.
// Codex rust-v0.162.1 core/src/unified_exec/mod.rs UNIFIED_EXEC_OUTPUT_MAX_BYTES.
const MaxBytes = 1024 * 1024

// Buffer keeps the first and last halves of a byte stream within a fixed budget and counts the
// bytes dropped between them.
// Codex rust-v0.162.1 core/src/unified_exec/head_tail_buffer.rs HeadTailBuffer.
type Buffer struct {
	maxBytes int
	head     []byte
	tail     []byte
	omitted  int
}

// New creates an empty output buffer with maxBytes of retained capacity.
func New(maxBytes int) *Buffer {
	return &Buffer{maxBytes: maxBytes}
}

// Restore rebuilds a buffer from retained head-then-tail bytes and their omission count. Once
// bytes have been omitted both halves are full, so their split is fixed by maxBytes.
func Restore(maxBytes int, retained []byte, omittedBytes int) (*Buffer, error) {
	buffer := New(maxBytes)
	if omittedBytes == 0 {
		if len(retained) > maxBytes {
			return nil, fmt.Errorf("retained output %d bytes exceeds %d without omission", len(retained), maxBytes)
		}
		buffer.Append(retained)
		return buffer, nil
	}
	if len(retained) != maxBytes {
		return nil, fmt.Errorf("retained output with omission must hold %d bytes, has %d", maxBytes, len(retained))
	}
	headBudget := buffer.maxBytes / 2
	buffer.head = append([]byte(nil), retained[:headBudget]...)
	buffer.tail = append([]byte(nil), retained[headBudget:]...)
	buffer.omitted = omittedBytes
	return buffer, nil
}

// Append adds output bytes while preserving the head and tail within the buffer's capacity.
func (b *Buffer) Append(chunk []byte) {
	headBudget := b.maxBytes / 2
	tailBudget := b.maxBytes - headBudget
	if room := headBudget - len(b.head); room > 0 {
		take := min(room, len(chunk))
		b.head = append(b.head, chunk[:take]...)
		chunk = chunk[take:]
	}
	excess := len(b.tail) + len(chunk) - tailBudget
	if excess > 0 {
		b.omitted += excess
		if excess >= len(b.tail) {
			chunk = chunk[excess-len(b.tail):]
			b.tail = b.tail[:0]
		} else {
			b.tail = b.tail[excess:]
		}
	}
	b.tail = append(b.tail, chunk...)
}

// RetainedBytes returns a copy of retained head bytes followed by retained tail bytes.
func (b *Buffer) RetainedBytes() []byte {
	return append(append([]byte(nil), b.head...), b.tail...)
}

// OmittedBytes returns the number of bytes dropped between the retained head and tail.
func (b *Buffer) OmittedBytes() int {
	return b.omitted
}

// TotalBytes returns retained bytes plus bytes omitted from the middle.
func (b *Buffer) TotalBytes() int {
	return len(b.head) + len(b.tail) + b.omitted
}

// WithOmissionMarker returns retained bytes with the marker line between the head and tail.
func (b *Buffer) WithOmissionMarker() []byte {
	if b.omitted == 0 {
		return b.RetainedBytes()
	}
	out := append([]byte(nil), b.head...)
	out = append(out, '\n')
	out = append(out, OmissionMarker(b.omitted)...)
	out = append(out, '\n')
	return append(out, b.tail...)
}

// OmissionMarker formats the model-facing marker for omitted output bytes.
// Codex rust-v0.162.1 core/src/unified_exec/mod.rs format_output_omission_marker.
func OmissionMarker(omittedBytes int) string {
	return fmt.Sprintf("... %d bytes omitted ...", omittedBytes)
}
