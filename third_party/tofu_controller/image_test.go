package tofucontroller

import (
	"archive/tar"
	"bytes"
	"debug/elf"
	"io"
	"os"
	"path"
	"testing"

	"github.com/bazelbuild/rules_go/go/runfiles"
)

func TestImageManagerIsNotRaceInstrumented(t *testing.T) {
	layerPath, err := runfiles.Rlocation(os.Getenv("TOFU_APP_LAYER"))
	if err != nil {
		t.Fatal(err)
	}
	layer, err := os.Open(layerPath)
	if err != nil {
		t.Fatal(err)
	}
	defer layer.Close()
	archive := tar.NewReader(layer)
	for {
		entry, err := archive.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if path.Base(entry.Name) != "tofu-controller" {
			continue
		}
		data, err := io.ReadAll(archive)
		if err != nil {
			t.Fatal(err)
		}
		binary, err := elf.NewFile(bytes.NewReader(data))
		if err != nil {
			t.Fatal(err)
		}
		defer binary.Close()
		// Production builds may be stripped, but Go's function-name table is retained.
		// Check its presence so removing that metadata cannot silently bypass this test.
		if !bytes.Contains(data, []byte("runtime.main")) {
			t.Fatal("image binary has no inspectable Go function names")
		}
		if bytes.Contains(data, []byte("runtime.raceinit")) {
			t.Fatal("production image contains the Go race detector")
		}
		return
	}
	t.Fatal("tofu-controller binary missing from image layer")
}
