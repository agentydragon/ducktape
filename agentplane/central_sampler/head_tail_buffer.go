package centralsampler

import "fmt"

// Codex rust-v0.162.1 core/src/unified_exec/mod.rs UNIFIED_EXEC_OUTPUT_MAX_BYTES.
const unifiedExecOutputMaxBytes = 1024 * 1024

// headTailBuffer keeps the first and last halves of a byte stream within a fixed budget and
// counts what it dropped between them.
// Codex rust-v0.162.1 core/src/unified_exec/head_tail_buffer.rs HeadTailBuffer.
type headTailBuffer struct {
	maxBytes int
	head     []byte
	tail     []byte
	omitted  int
}

func newHeadTailBuffer(maxBytes int) *headTailBuffer {
	return &headTailBuffer{maxBytes: maxBytes}
}

// restoreHeadTailBuffer rebuilds a buffer from its retained bytes and omission count. Retained
// bytes are head then tail; once anything was omitted both halves are full, so the split is fixed.
func restoreHeadTailBuffer(maxBytes int, retained []byte, omitted int) (*headTailBuffer, error) {
	buffer := newHeadTailBuffer(maxBytes)
	if omitted == 0 {
		if len(retained) > maxBytes {
			return nil, fmt.Errorf("retained output %d bytes exceeds %d without omission", len(retained), maxBytes)
		}
		buffer.push(retained)
		return buffer, nil
	}
	if len(retained) != maxBytes {
		return nil, fmt.Errorf("retained output with omission must hold %d bytes, has %d", maxBytes, len(retained))
	}
	headBudget := buffer.headBudget()
	buffer.head = append([]byte(nil), retained[:headBudget]...)
	buffer.tail = append([]byte(nil), retained[headBudget:]...)
	buffer.omitted = omitted
	return buffer, nil
}

func (b *headTailBuffer) headBudget() int { return b.maxBytes / 2 }

func (b *headTailBuffer) tailBudget() int { return b.maxBytes - b.headBudget() }

func (b *headTailBuffer) push(chunk []byte) {
	if room := b.headBudget() - len(b.head); room > 0 {
		take := min(room, len(chunk))
		b.head = append(b.head, chunk[:take]...)
		chunk = chunk[take:]
	}
	excess := len(b.tail) + len(chunk) - b.tailBudget()
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

func (b *headTailBuffer) totalBytes() int { return len(b.head) + len(b.tail) + b.omitted }

func (b *headTailBuffer) retained() []byte {
	return append(append([]byte(nil), b.head...), b.tail...)
}

// withOmissionMarker is the model-facing byte view: head, marker line, tail.
func (b *headTailBuffer) withOmissionMarker() []byte {
	if b.omitted == 0 {
		return b.retained()
	}
	out := append([]byte(nil), b.head...)
	out = append(out, '\n')
	out = append(out, outputOmissionMarker(b.omitted)...)
	out = append(out, '\n')
	return append(out, b.tail...)
}

// Codex rust-v0.162.1 core/src/unified_exec/mod.rs format_output_omission_marker.
func outputOmissionMarker(omittedBytes int) string {
	return fmt.Sprintf("... %d bytes omitted ...", omittedBytes)
}
