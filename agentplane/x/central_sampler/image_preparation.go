package centralsampler

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	_ "image/gif"
	"image/jpeg"
	"image/png"
	"log/slog"
	"math"
	"strings"

	"golang.org/x/image/draw"
	_ "golang.org/x/image/webp"
)

// Codex rust-v0.162.1 core/src/image_preparation.rs prepares tool-output images before they
// enter history, here with its defaults: ImagePreparationMode::DetailBased and no resize notice.
// The decoders registered above are upstream's image crate features: jpeg, png, gif, webp.
const (
	imageProcessingErrorPlaceholder = "image content omitted because it could not be processed"
	imageTooLargePlaceholder        = "image content omitted because it exceeded the supported size limit; use a smaller image"
	unsupportedLowDetailPlaceholder = "image content omitted because detail 'low' is not supported; use 'high', 'original', or 'auto'"
	remoteImageURLPlaceholder       = "image content omitted because remote image URLs are not supported"
)

// Codex rust-v0.162.1 utils/image/src/lib.rs.
const (
	promptImagePatchSize     = 32
	maxPromptImageInputBytes = 1024 * 1024 * 1024
)

type promptImageLimits struct {
	maxDimension int
	maxPatches   int
}

// Codex rust-v0.162.1 utils/image/src/lib.rs PromptImageMode::{HIGH_DETAIL,ORIGINAL_DETAIL}.
var (
	highDetailLimits     = promptImageLimits{maxDimension: 2048, maxPatches: 2500}
	originalDetailLimits = promptImageLimits{maxDimension: 6000, maxPatches: 10000}
)

// imagePreparationError replaces its image with the placeholder text.
type imagePreparationError struct {
	placeholder string
	err         error
}

func (e *imagePreparationError) Error() string {
	return fmt.Sprintf("prepare image (%s): %v", e.placeholder, e.err)
}

func (e *imagePreparationError) Unwrap() error { return e.err }

// prepareOutputImages replaces each inline input_image with its prompt-prepared form, or with
// upstream's placeholder text when it cannot be prepared.
//
// TODO: Consider upstream's attachment store (core/src/image_preparation.rs prepare_image uploads
// prepared bytes and may reference them by file_id). Prepared images stay inline here, so every
// provider request and every sampling intent's journaled body carries their full base64 again.
// The 32 MiB view_image cap (view_image.go) also differs from upstream, which has none and
// fails only at the 64 MiB WebSocket message or code-mode frame bounds.
func prepareOutputImages(parts []ResponsesContentPart) ([]ResponsesContentPart, error) {
	for index, part := range parts {
		if part.Type != "input_image" {
			continue
		}
		prepared, err := prepareOutputImage(part)
		if failed := (*imagePreparationError)(nil); errors.As(err, &failed) {
			slog.Warn("failed to prepare tool output image", "error", err)
			parts[index] = ResponsesContentPart{Type: "input_text", Text: failed.placeholder}
			continue
		}
		if err != nil {
			return nil, err
		}
		parts[index] = prepared
	}
	return parts, nil
}

// Codex rust-v0.162.1 core/src/image_preparation.rs resize_image.
func prepareOutputImage(part ResponsesContentPart) (ResponsesContentPart, error) {
	var fields map[string]json.RawMessage
	if err := decodeProtocolObject(part.Raw, &fields); err != nil {
		return ResponsesContentPart{}, err
	}
	rawURL, inline := fields["image_url"]
	if !inline {
		return part, nil
	}
	var imageURL string
	if err := json.Unmarshal(rawURL, &imageURL); err != nil {
		return ResponsesContentPart{}, fmt.Errorf("input_image image_url: %w", err)
	}
	scheme, _, _ := strings.Cut(imageURL, ":")
	if strings.EqualFold(scheme, "http") || strings.EqualFold(scheme, "https") {
		return ResponsesContentPart{}, &imagePreparationError{placeholder: remoteImageURLPlaceholder, err: errors.New("remote image URLs are not supported")}
	}
	if !hasFoldedPrefix(imageURL, "data:") {
		return part, nil
	}
	var detail *string
	if raw, ok := fields["detail"]; ok {
		if err := json.Unmarshal(raw, &detail); err != nil {
			return ResponsesContentPart{}, fmt.Errorf("input_image detail: %w", err)
		}
	}
	limits := highDetailLimits
	if detail != nil {
		switch *detail {
		case "auto", "high":
		case "original":
			limits = originalDetailLimits
		case "low":
			return ResponsesContentPart{}, &imagePreparationError{placeholder: unsupportedLowDetailPlaceholder, err: errors.New("image detail `low` is not supported")}
		default:
			return ResponsesContentPart{}, fmt.Errorf("unsupported input_image detail %q", *detail)
		}
	}
	mime, data, err := loadDataURLForPrompt(imageURL, limits)
	if err != nil {
		return ResponsesContentPart{}, err
	}
	encodedURL, err := json.Marshal("data:" + mime + ";base64," + base64.StdEncoding.EncodeToString(data))
	if err != nil {
		return ResponsesContentPart{}, err
	}
	fields["image_url"] = encodedURL
	raw, err := json.Marshal(fields)
	if err != nil {
		return ResponsesContentPart{}, err
	}
	var prepared ResponsesContentPart
	if err := json.Unmarshal(raw, &prepared); err != nil {
		return ResponsesContentPart{}, err
	}
	return prepared, nil
}

func hasFoldedPrefix(value, prefix string) bool {
	return len(value) >= len(prefix) && strings.EqualFold(value[:len(prefix)], prefix)
}

// Codex rust-v0.162.1 utils/image/src/lib.rs load_data_url_for_prompt_with.
func loadDataURLForPrompt(imageURL string, limits promptImageLimits) (string, []byte, error) {
	processing := func(err error) error {
		return &imagePreparationError{placeholder: imageProcessingErrorPlaceholder, err: err}
	}
	metadata, encoded, found := strings.Cut(imageURL[len("data:"):], ",")
	if !found {
		return "", nil, processing(errors.New("invalid data URL: missing comma separator"))
	}
	base64Encoded := false
	for _, part := range strings.Split(metadata, ";") {
		base64Encoded = base64Encoded || strings.EqualFold(part, "base64")
	}
	if !base64Encoded {
		return "", nil, processing(errors.New("invalid data URL: only base64 data URLs are supported"))
	}
	if len(encoded) > maxPromptImageInputBytes {
		return "", nil, &imagePreparationError{placeholder: imageTooLargePlaceholder, err: fmt.Errorf("base64 payload is %d bytes", len(encoded))}
	}
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return "", nil, processing(fmt.Errorf("invalid data URL: invalid base64 payload: %w", err))
	}
	mime, prepared, err := loadForPrompt(data, limits)
	if err != nil {
		return "", nil, processing(err)
	}
	return mime, prepared, nil
}

// Codex rust-v0.162.1 utils/image/src/lib.rs load_for_prompt_bytes_uncached with
// PromptImageMode::ResizeWithLimits.
// Deviation: re-encoded images drop ICC and EXIF metadata, and a resized WebP becomes PNG
// because Go has no WebP encoder. Unresized PNG, JPEG and WebP bytes pass through unchanged
// as upstream.
func loadForPrompt(data []byte, limits promptImageLimits) (string, []byte, error) {
	decoded, format, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return "", nil, err
	}
	bounds := decoded.Bounds()
	width, height := promptImageOutputDimensions(bounds.Dx(), bounds.Dy(), limits)
	preservable := format == "png" || format == "jpeg" || format == "webp"
	if width == bounds.Dx() && height == bounds.Dy() {
		if preservable {
			return "image/" + format, data, nil
		}
		return encodePromptImage(decoded, "png")
	}
	resized := image.NewRGBA(image.Rect(0, 0, width, height))
	// Bilinear is upstream's FilterType::Triangle.
	draw.BiLinear.Scale(resized, resized.Bounds(), decoded, bounds, draw.Src, nil)
	if format == "jpeg" {
		return encodePromptImage(resized, "jpeg")
	}
	return encodePromptImage(resized, "png")
}

func encodePromptImage(img image.Image, format string) (string, []byte, error) {
	var buffer bytes.Buffer
	var err error
	if format == "jpeg" {
		err = jpeg.Encode(&buffer, img, &jpeg.Options{Quality: 85})
	} else {
		err = png.Encode(&buffer, img)
	}
	if err != nil {
		return "", nil, err
	}
	return "image/" + format, buffer.Bytes(), nil
}

// Codex rust-v0.162.1 utils/image/src/lib.rs prompt_image_output_dimensions_for_limits.
func promptImageOutputDimensions(width, height int, limits promptImageLimits) (int, int) {
	width, height = max(width, 1), max(height, 1)
	if promptImageDimensionsFit(width, height, limits) {
		return width, height
	}
	scale := math.Min(float64(limits.maxDimension)/float64(max(width, height)), 1)
	width = max(int(math.Round(float64(width)*scale)), 1)
	height = max(int(math.Round(float64(height)*scale)), 1)
	if promptImageDimensionsFit(width, height, limits) {
		return width, height
	}
	widthF, heightF, patch := float64(width), float64(height), float64(promptImagePatchSize)
	scale = math.Sqrt(patch * patch * float64(limits.maxPatches) / widthF / heightF)
	// Match Responses patch-budget math: shrink by area, then round the scaled patch grid down
	// so integer output dimensions remain within the budget.
	patchesWide, patchesHigh := widthF*scale/patch, heightF*scale/patch
	scale *= math.Min(math.Floor(patchesWide)/patchesWide, math.Floor(patchesHigh)/patchesHigh)
	return max(int(math.Floor(widthF*scale)), 1), max(int(math.Floor(heightF*scale)), 1)
}

func promptImageDimensionsFit(width, height int, limits promptImageLimits) bool {
	patches := ((width + promptImagePatchSize - 1) / promptImagePatchSize) * ((height + promptImagePatchSize - 1) / promptImagePatchSize)
	return width <= limits.maxDimension && height <= limits.maxDimension && patches <= limits.maxPatches
}
