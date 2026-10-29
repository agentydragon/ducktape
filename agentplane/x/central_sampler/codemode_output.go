package centralsampler

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"time"
)

// Codex rust-v0.162.1 utils/string/src/truncate.rs APPROX_BYTES_PER_TOKEN.
const approxBytesPerToken = 4

// cellOutput renders an exec or wait result like Codex rust-v0.162.1
// core/src/tools/code_mode/mod.rs handle_runtime_response and output.rs CodeModeToolOutput: the
// script error joins the content items, the token budget truncates them, then the status header
// goes first. cellHandle is the model-facing cell ID.
func cellOutput(observation codeObservation, cellHandle string, maxTokens *int, wallTime time.Duration) ([]ResponsesContentPart, error) {
	var status string
	switch observation.Kind {
	case codeYielded:
		status = "Script running with cell ID " + cellHandle
	case codeTerminated:
		status = "Script terminated"
	case codeResult:
		status = "Script completed"
		if observation.Cell.ErrorText != nil {
			status = "Script failed"
		}
	default:
		return nil, fmt.Errorf("unsupported cell observation %q", observation.Kind)
	}
	items := make([]ResponsesContentPart, 0, len(observation.Cell.ContentItems)+2)
	for _, raw := range observation.Cell.ContentItems {
		var item ResponsesContentPart
		if err := json.Unmarshal(raw, &item); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if observation.Kind == codeResult && observation.Cell.ErrorText != nil {
		items = append(items, ResponsesContentPart{Type: "input_text", Text: "Script error:\n" + *observation.Cell.ErrorText})
	}
	budget := defaultCodeModeMaxOutputTokens
	if maxTokens != nil {
		budget = *maxTokens
	}
	items, err := truncateCodeModeResult(items, budget)
	if err != nil {
		return nil, err
	}
	seconds := math.Round(wallTime.Seconds()*10) / 10
	header := ResponsesContentPart{Type: "input_text", Text: fmt.Sprintf("%s\nWall time %.1f seconds\nOutput:\n", status, seconds)}
	return append([]ResponsesContentPart{header}, items...), nil
}

// Codex rust-v0.162.1 core/src/tools/code_mode/mod.rs truncate_code_mode_result with
// TruncationPolicy::Tokens.
func truncateCodeModeResult(items []ResponsesContentPart, maxTokens int) ([]ResponsesContentPart, error) {
	for _, item := range items {
		if item.Type != "input_text" {
			return truncateOutputItems(items, maxTokens)
		}
	}
	return formattedTruncateTextItems(items, maxTokens), nil
}

// Codex rust-v0.162.1 utils/output-truncation/src/lib.rs
// formatted_truncate_text_content_items_with_policy for all-text content.
func formattedTruncateTextItems(items []ResponsesContentPart, maxTokens int) []ResponsesContentPart {
	if len(items) == 0 {
		return items
	}
	texts := make([]string, 0, len(items))
	for _, item := range items {
		texts = append(texts, item.Text)
	}
	combined := strings.Join(texts, "\n")
	if len(combined) <= maxTokens*approxBytesPerToken {
		return items
	}
	// formatted_truncate_text
	text := fmt.Sprintf("Warning: truncated output (original token count: %d)\nTotal output lines: %d\n\n%s",
		approxTokenCount(combined), countLines(combined), truncateMiddleWithTokenBudget(combined, maxTokens))
	return []ResponsesContentPart{{Type: "input_text", Text: text}}
}

// Codex rust-v0.162.1 utils/output-truncation/src/lib.rs truncate_function_output_items_with_policy
// with TruncationPolicy::Tokens. Audio uses estimate_audio_token_count's size fallback; this
// service does not decode audio duration.
func truncateOutputItems(items []ResponsesContentPart, maxTokens int) ([]ResponsesContentPart, error) {
	out := make([]ResponsesContentPart, 0, len(items))
	remaining := maxTokens
	omittedText, omittedAudio := 0, 0
	for _, item := range items {
		switch item.Type {
		case "input_text":
			if item.Text == "" {
				continue
			}
			if remaining == 0 {
				omittedText++
				continue
			}
			if cost := approxTokenCount(item.Text); cost <= remaining {
				out = append(out, ResponsesContentPart{Type: "input_text", Text: item.Text})
				remaining -= cost
			} else {
				if snippet := truncateMiddleWithTokenBudget(item.Text, remaining); snippet == "" {
					omittedText++
				} else {
					out = append(out, ResponsesContentPart{Type: "input_text", Text: snippet})
				}
				remaining = 0
			}
		case "input_audio":
			var audio struct {
				AudioURL string `json:"audio_url"`
			}
			if err := json.Unmarshal(item.Raw, &audio); err != nil {
				return nil, err
			}
			if cost := approxTokenCount(audio.AudioURL); cost <= remaining {
				out = append(out, item)
				remaining -= cost
			} else {
				omittedAudio++
			}
		default:
			out = append(out, item)
		}
	}
	if omittedText > 0 {
		out = append(out, ResponsesContentPart{Type: "input_text", Text: fmt.Sprintf("[omitted %d text items ...]", omittedText)})
	}
	if omittedAudio > 0 {
		out = append(out, ResponsesContentPart{Type: "input_text", Text: fmt.Sprintf("[omitted %d audio items ...]", omittedAudio)})
	}
	return out, nil
}

// Codex rust-v0.162.1 utils/string/src/truncate.rs approx_token_count.
func approxTokenCount(text string) int {
	return (len(text) + approxBytesPerToken - 1) / approxBytesPerToken
}

// countLines matches Rust str::lines: a trailing newline does not start another line.
func countLines(text string) int {
	if text == "" {
		return 0
	}
	return strings.Count(strings.TrimSuffix(text, "\n"), "\n") + 1
}

// Codex rust-v0.162.1 utils/string/src/truncate.rs truncate_middle_with_token_budget (string
// result only) via truncate_with_byte_estimate with use_tokens.
func truncateMiddleWithTokenBudget(text string, maxTokens int) string {
	if text == "" {
		return ""
	}
	maxBytes := maxTokens * approxBytesPerToken
	if maxTokens > 0 && len(text) <= maxBytes {
		return text
	}
	if maxBytes == 0 {
		return fmt.Sprintf("…%d tokens truncated…", approxTokenCount(text))
	}
	if len(text) <= maxBytes {
		return text
	}
	left := maxBytes / 2
	prefixEnd := floorCharBoundary(text, left)
	suffixStart := ceilCharBoundary(text, len(text)-(maxBytes-left))
	// approx_tokens_from_byte_count of the removed byte count.
	removed := (len(text) - maxBytes + approxBytesPerToken - 1) / approxBytesPerToken
	return text[:prefixEnd] + fmt.Sprintf("…%d tokens truncated…", removed) + text[suffixStart:]
}

func floorCharBoundary(text string, index int) int {
	for index > 0 && index < len(text) && !isCharBoundary(text[index]) {
		index--
	}
	return index
}

func ceilCharBoundary(text string, index int) int {
	for index < len(text) && !isCharBoundary(text[index]) {
		index++
	}
	return index
}

// isCharBoundary reports whether a byte starts a UTF-8 sequence (is not a continuation byte).
func isCharBoundary(b byte) bool { return b&0xC0 != 0x80 }
