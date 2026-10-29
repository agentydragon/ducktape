package centralsampler

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// lossyUTF8 replaces each invalid byte with U+FFFD, as Rust String::from_utf8_lossy does for
// single-byte faults (Rust folds a truncated multi-byte prefix into one replacement).
func lossyUTF8(data []byte) string {
	if utf8.Valid(data) {
		return string(data)
	}
	var out strings.Builder
	for len(data) > 0 {
		r, size := utf8.DecodeRune(data)
		out.WriteRune(r)
		data = data[size:]
	}
	return out.String()
}

// truncateExecOutput applies a model-requested token budget to collected process output.
// Codex rust-v0.162.1 core/src/tools/context.rs ExecCommandToolOutput::truncated_output_with_policy
// and utils/output-truncation/src/lib.rs formatted_truncate_text.
func truncateExecOutput(output []byte, omittedBytes int, originalTokenCount int, maxTokens int) string {
	text := lossyUTF8(output)
	budget := maxTokens * approxBytesPerToken
	if omittedBytes == 0 {
		if len(text) <= budget {
			return text
		}
		return fmt.Sprintf("Warning: truncated output (original token count: %d)\nTotal output lines: %d\n\n%s",
			approxTokenCount(text), countLines(text), truncateMiddleWithTokenBudget(text, maxTokens))
	}
	marker := outputOmissionMarker(omittedBytes)
	if len(text) <= budget {
		if strings.Contains(text, marker) {
			return text
		}
		return marker + "\n" + text
	}
	truncated := truncateMiddleWithTokenBudget(text, maxTokens)
	notice := ""
	if !strings.Contains(truncated, marker) {
		notice = marker + "\n"
	}
	return fmt.Sprintf("Warning: truncated output (original token count: %d)\n%s\n%s", originalTokenCount, notice, truncated)
}
