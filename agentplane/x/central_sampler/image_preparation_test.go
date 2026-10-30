package centralsampler

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"strings"
	"testing"
)

func TestPromptImageOutputDimensions(t *testing.T) {
	for _, tc := range []struct {
		name          string
		width, height int
		limits        promptImageLimits
		want          [2]int
	}{
		{"within high detail", 1000, 500, highDetailLimits, [2]int{1000, 500}},
		{"longest side bound", 3000, 1000, highDetailLimits, [2]int{2048, 683}},
		// 2048x2048 is 64x64 = 4096 patches; scaling by area to 2500 patches gives 50x50.
		{"patch budget", 2048, 2048, highDetailLimits, [2]int{1600, 1600}},
		{"within original detail", 3000, 1000, originalDetailLimits, [2]int{3000, 1000}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			width, height := promptImageOutputDimensions(tc.width, tc.height, tc.limits)
			if [2]int{width, height} != tc.want {
				t.Fatalf("dimensions = %dx%d, want %dx%d", width, height, tc.want[0], tc.want[1])
			}
		})
	}
}

func encodedImage(t *testing.T, width, height int, encode func(*bytes.Buffer, image.Image) error) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, width, height))
	for x := range width {
		img.Set(x, 0, color.RGBA{R: 255, A: 255})
	}
	var buffer bytes.Buffer
	if err := encode(&buffer, img); err != nil {
		t.Fatal(err)
	}
	return buffer.Bytes()
}

func encodePNG(buffer *bytes.Buffer, img image.Image) error { return png.Encode(buffer, img) }

func encodeJPEG(buffer *bytes.Buffer, img image.Image) error { return jpeg.Encode(buffer, img, nil) }

func imagePart(t *testing.T, fields map[string]any) ContentPart {
	t.Helper()
	var part ContentPart
	if err := json.Unmarshal(mustJSON(fields), &part); err != nil {
		t.Fatal(err)
	}
	return part
}

func dataURL(mime string, data []byte) string {
	return "data:" + mime + ";base64," + base64.StdEncoding.EncodeToString(data)
}

func TestPrepareOutputImagesResizesAndRelabels(t *testing.T) {
	oversized := encodedImage(t, 3000, 1000, encodePNG)
	small := encodedImage(t, 64, 32, encodeJPEG)
	parts, err := prepareOutputImages([]ContentPart{
		imagePart(t, map[string]any{"type": "input_image", "image_url": dataURL("application/octet-stream", oversized), "detail": "high"}),
		imagePart(t, map[string]any{"type": "input_image", "image_url": dataURL("application/octet-stream", small)}),
		imagePart(t, map[string]any{"type": "input_image", "file_id": "file-test"}),
	})
	if err != nil {
		t.Fatal(err)
	}
	var resized struct {
		ImageURL string `json:"image_url"`
		Detail   string `json:"detail"`
	}
	if err := json.Unmarshal(parts[0].Raw, &resized); err != nil {
		t.Fatal(err)
	}
	encoded, found := strings.CutPrefix(resized.ImageURL, "data:image/png;base64,")
	if !found || resized.Detail != "high" {
		t.Fatalf("resized part = %s", parts[0].Raw)
	}
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatal(err)
	}
	config, err := png.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if config.Width != 2048 || config.Height != 683 {
		t.Fatalf("resized to %dx%d", config.Width, config.Height)
	}
	var passed struct {
		ImageURL string `json:"image_url"`
	}
	if err := json.Unmarshal(parts[1].Raw, &passed); err != nil {
		t.Fatal(err)
	}
	if passed.ImageURL != dataURL("image/jpeg", small) {
		t.Fatal("an image within bounds must keep its bytes and gain its real MIME type")
	}
	if !bytes.Equal(parts[2].Raw, mustJSON(map[string]any{"type": "input_image", "file_id": "file-test"})) {
		t.Fatalf("file reference changed: %s", parts[2].Raw)
	}
}

func TestPrepareOutputImagesReplacesUnpreparableImages(t *testing.T) {
	for _, tc := range []struct {
		name        string
		part        map[string]any
		placeholder string
	}{
		{"remote URL", map[string]any{"type": "input_image", "image_url": "https://example.invalid/image.png"}, remoteImageURLPlaceholder},
		{"low detail", map[string]any{"type": "input_image", "image_url": dataURL("image/png", encodedImage(t, 4, 4, encodePNG)), "detail": "low"}, unsupportedLowDetailPlaceholder},
		{"not an image", map[string]any{"type": "input_image", "image_url": dataURL("image/png", []byte("not an image"))}, imageProcessingErrorPlaceholder},
		{"not base64", map[string]any{"type": "input_image", "image_url": "data:image/png,raw"}, imageProcessingErrorPlaceholder},
	} {
		t.Run(tc.name, func(t *testing.T) {
			parts, err := prepareOutputImages([]ContentPart{imagePart(t, tc.part)})
			if err != nil {
				t.Fatal(err)
			}
			if parts[0].Type != "input_text" || parts[0].Text != tc.placeholder {
				t.Fatalf("part = %+v", parts[0])
			}
		})
	}
}
