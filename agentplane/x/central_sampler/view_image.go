package centralsampler

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"log/slog"
	"path/filepath"
	"strings"

	"github.com/google/uuid"
)

const (
	// Deviation: upstream reads whole files with fs/readFile. Its base64 reply exceeds the
	// Runner relay's 1 MiB message bound for files above ~750 KiB, so reads use fs/readBlock.
	viewImageBlockBytes = 512 * 1024
	// Deviation: upstream has no view_image size limit. The result travels to the host as one
	// base64 code-mode frame, bounded by maxCodeModeFrame.
	maxViewImageBytes = 32 * 1024 * 1024
)

// Codex rust-v0.162.1 core/src/tools/handlers/view_image.rs VIEW_IMAGE_INVALID_MESSAGE
const viewImageInvalidMessage = "unable to process image: invalid or unsupported image data"

// Codex rust-v0.162.1 core/src/tools/handlers/view_image_spec.rs create_view_image_tool with
// ViewImageToolOptions all false: the sampler has no model catalog to enable original detail.
func viewImageToolDefinition() ResponsesCodeToolDefinition {
	closed := false
	detail := schemaString("Image detail hint returned by view_image. Returns `high` for default resized behavior or `original` when original resolution is preserved.")
	detail.Enum = []string{"high", "original"}
	return ResponsesCodeToolDefinition{
		Name: "view_image", ToolName: ResponsesToolName{Name: "view_image"},
		Description: "View a local image file from the filesystem when visual inspection is needed. Use this for images already available on disk.",
		Kind:        "function",
		InputSchema: &ResponsesJSONSchema{
			Type: "object", Required: []string{"path"}, AdditionalProperties: &closed,
			Properties: map[string]ResponsesJSONSchema{"path": schemaString("Local filesystem path to an image file.")},
		},
		OutputSchema: &ResponsesJSONSchema{
			Type: "object", Required: []string{"image_url", "detail"}, AdditionalProperties: &closed,
			Properties: map[string]ResponsesJSONSchema{"image_url": schemaString("Data URL for the loaded image."), "detail": detail},
		},
	}
}

// Codex rust-v0.162.1 core/src/tools/handlers/view_image.rs ViewImageArgs. Upstream also accepts
// environment_id; the sampler has one environment, so like other unknown fields it is ignored.
type viewImageInput struct {
	Path   *string `json:"path"`
	Detail *string `json:"detail"`
}

// Codex rust-v0.162.1 core/src/tools/handlers/view_image.rs ViewImageOutput::code_mode_result.
// The image URL holds the file's bytes unchanged; observe prepares images the cell emits.
type viewImageResult struct {
	ImageURL string `json:"image_url"`
	Detail   string `json:"detail"`
}

// evidence journals a digest instead of the data URL, which the conversation retains if the
// cell emits it.
func (r *viewImageResult) evidence() map[string]any {
	return map[string]any{"image_url": omittedDataURL(r.ImageURL), "detail": r.Detail}
}

func omittedDataURL(url string) string {
	digest := sha256.Sum256([]byte(url))
	return fmt.Sprintf("<image data URL omitted: %d bytes, sha256:%s>", len(url), hex.EncodeToString(digest[:]))
}

// Codex rust-v0.162.1 core/src/tools/handlers/view_image.rs ViewImageHandler::handle_call.
func (e *Executor) viewImage(ctx context.Context, arguments json.RawMessage) (*viewImageResult, error) {
	var args viewImageInput
	if err := decodeFunctionArguments("view_image", arguments, &args); err != nil {
		return nil, err
	}
	if args.Path == nil {
		return nil, missingField("path")
	}
	if args.Detail != nil && *args.Detail != "high" && *args.Detail != "original" {
		return nil, &toolError{fmt.Sprintf(
			"view_image.detail only supports `high` or `original`; omit `detail` for default high resized behavior, got `%s`", *args.Detail,
		)}
	}
	path := *args.Path
	if !filepath.IsAbs(path) {
		path = filepath.Join(e.cwd, path)
	}
	metadata, err := executorRPC[executorFSMetadata](ctx, e, "fs/getMetadata", executorFSGetMetadata{Path: fileURI(path)})
	if rejected := (*executorRejectedError)(nil); errors.As(err, &rejected) {
		return nil, &toolError{fmt.Sprintf("unable to locate image at `%s`: %s", path, rejected.message)}
	} else if err != nil {
		return nil, err
	}
	if !metadata.IsFile {
		return nil, &toolError{fmt.Sprintf("image path `%s` is not a file", path)}
	}
	if metadata.Size > maxViewImageBytes {
		return nil, &toolError{fmt.Sprintf("image at `%s` is %d bytes; view_image reads at most %d bytes", path, metadata.Size, maxViewImageBytes)}
	}
	data, err := e.readFile(ctx, path)
	if rejected := (*executorRejectedError)(nil); errors.As(err, &rejected) {
		return nil, &toolError{fmt.Sprintf("unable to read image at `%s`: %s", path, rejected.message)}
	} else if err != nil {
		return nil, err
	}
	// Reject non-images before their bytes reach the cell; valid bytes pass through unchanged.
	// image_preparation.go registers upstream's decoder set.
	if _, _, err := image.Decode(bytes.NewReader(data)); err != nil {
		return nil, &toolError{viewImageInvalidMessage}
	}
	// Original detail needs model support the sampler cannot see; upstream then returns high.
	return &viewImageResult{ImageURL: "data:application/octet-stream;base64," + base64.StdEncoding.EncodeToString(data), Detail: "high"}, nil
}

// readFile streams a file through fs/open and fs/readBlock, journaling block digests rather than
// contents.
func (e *Executor) readFile(ctx context.Context, path string) ([]byte, error) {
	// Codex rust-v0.162.1 exec-server/src/remote_file_stream.rs open: a simple UUID fits the
	// server's 32-byte handle limit.
	handle := strings.ReplaceAll(uuid.NewString(), "-", "")
	if _, err := executorRPC[executorFSOpened](ctx, e, "fs/open", executorFSOpen{HandleID: handle, Path: fileURI(path), Mode: "read"}); err != nil {
		return nil, err
	}
	data, err := e.readBlocks(ctx, handle)
	_, closeErr := executorRPC[executorEmpty](ctx, e, "fs/close", executorFSClose{HandleID: handle})
	if rejected := (*executorRejectedError)(nil); errors.As(closeErr, &rejected) {
		// Upstream also ignores close failures; the rejection is journaled.
		slog.Warn("exec-server rejected fs/close", "handle", handle, "error", closeErr)
	} else if err == nil {
		err = closeErr
	}
	return data, err
}

func (e *Executor) readBlocks(ctx context.Context, handle string) ([]byte, error) {
	var data []byte
	for {
		block, err := executorRPCWithEvidence[executorFSBlock](ctx, e, "fs/readBlock",
			executorFSReadBlock{HandleID: handle, Offset: uint64(len(data)), Len: viewImageBlockBytes}, blockEvidence)
		if err != nil {
			return nil, err
		}
		if len(block.Chunk) > viewImageBlockBytes {
			return nil, fmt.Errorf("fs/readBlock returned %d bytes; requested at most %d", len(block.Chunk), viewImageBlockBytes)
		}
		data = append(data, block.Chunk...)
		if len(data) > maxViewImageBytes {
			return nil, &toolError{fmt.Sprintf("image grew past %d bytes while being read", maxViewImageBytes)}
		}
		if block.EOF {
			return data, nil
		}
		if len(block.Chunk) == 0 {
			return nil, errors.New("fs/readBlock returned an empty non-terminal block")
		}
	}
}

func blockEvidence(result map[string]any) map[string]any {
	if chunk, ok := result["chunk"].(string); ok {
		digest := sha256.Sum256([]byte(chunk))
		result["chunk"] = map[string]any{"base64_bytes": len(chunk), "base64_sha256": hex.EncodeToString(digest[:])}
	}
	return result
}
