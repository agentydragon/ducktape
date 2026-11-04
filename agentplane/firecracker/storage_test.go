package firecracker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestDigestSparseFilesMatchesLogicalContents(t *testing.T) {
	for _, test := range []struct {
		name   string
		size   int
		writes map[int]string
	}{
		{name: "empty"},
		{name: "all holes", size: 3*1024*1024 + 19},
		{name: "data", size: 13, writes: map[int]string{0: "hello, world!"}},
		{name: "leading hole", size: 3*1024*1024 + 19, writes: map[int]string{2*1024*1024 + 3: "data after hole"}},
		{name: "trailing hole", size: 3*1024*1024 + 19, writes: map[int]string{0: "data before hole"}},
		{name: "mixed extents", size: 6*1024*1024 + 19, writes: map[int]string{3: "first", 2*1024*1024 + 17: "middle", 6*1024*1024 + 7: "last"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "disk.img")
			file, err := os.Create(path)
			if err != nil {
				t.Fatal(err)
			}
			contents := make([]byte, test.size)
			for offset, value := range test.writes {
				if _, err := file.WriteAt([]byte(value), int64(offset)); err != nil {
					t.Fatal(err)
				}
				copy(contents[offset:], value)
			}
			if err := file.Truncate(int64(test.size)); err != nil {
				t.Fatal(err)
			}
			if err := file.Close(); err != nil {
				t.Fatal(err)
			}
			actual, err := digest(path)
			if err != nil {
				t.Fatal(err)
			}
			want := sha256.Sum256(contents)
			if actual.Size != int64(test.size) || actual.SHA256 != hex.EncodeToString(want[:]) {
				t.Fatalf("digest = %+v, want size %d and SHA256 %x", actual, test.size, want)
			}
		})
	}
}

func BenchmarkDigestSparseWorkspace(b *testing.B) {
	path := filepath.Join(b.TempDir(), "workspace.img")
	file, err := os.Create(path)
	if err != nil {
		b.Fatal(err)
	}
	const size = 8 * 1024 * 1024 * 1024
	if _, err := file.WriteAt([]byte("retained workspace contents"), 1024*1024); err != nil {
		b.Fatal(err)
	}
	if err := file.Truncate(size); err != nil {
		b.Fatal(err)
	}
	if err := file.Close(); err != nil {
		b.Fatal(err)
	}
	for _, sparse := range []bool{false, true} {
		b.Run(fmt.Sprintf("sparse-aware=%t", sparse), func(b *testing.B) {
			b.SetBytes(size)
			b.ReportAllocs()
			for b.Loop() {
				file, err := os.Open(path)
				if err != nil {
					b.Fatal(err)
				}
				hash := sha256.New()
				var count int64
				if sparse {
					count, err = hashFileContents(file, hash)
				} else {
					count, err = io.CopyBuffer(hash, file, make([]byte, 1024*1024))
				}
				closeErr := file.Close()
				if err := errors.Join(err, closeErr); err != nil {
					b.Fatal(err)
				}
				if count != size {
					b.Fatalf("hashed %d bytes, want %d", count, size)
				}
			}
		})
	}
}

func testStorage(t *testing.T) *Storage {
	t.Helper()
	root := t.TempDir()
	bootDirectory := filepath.Join(root, "boot")
	runnerDirectory := filepath.Join(root, "runner")
	for _, directory := range []string{
		bootDirectory,
		filepath.Join(runnerDirectory, "disk"),
	} {
		if err := os.MkdirAll(directory, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	for path, content := range map[string]string{
		filepath.Join(bootDirectory, "vmlinux"):              "kernel source",
		filepath.Join(bootDirectory, "rootfs.ext4"):          "rootfs source",
		filepath.Join(runnerDirectory, "disk", "runner.img"): "runner source",
	} {
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	binary := filepath.Join(root, "firecracker")
	if err := os.WriteFile(binary, []byte("pinned test VMM"), 0o700); err != nil {
		t.Fatal(err)
	}
	config := Config{
		PodUID:             "pod-test",
		SandboxUID:         "sandbox-test",
		VCPUCount:          1,
		MemoryMiB:          256,
		BootImage:          "registry.invalid/boot@sha256:abc",
		RunnerImage:        "registry.invalid/runner@sha256:def",
		BootDirectory:      bootDirectory,
		RunnerDirectory:    runnerDirectory,
		WorkspaceKind:      WorkspaceKindRawDiskImage,
		StateSizeMiB:       256,
		WorkspaceSizeMiB:   256,
		RelayPort:          3128,
		PlatformRoutePorts: []int{},
	}
	storage := NewStorage(config, binary)
	storage.Work = filepath.Join(root, "state", "firecracker")
	storage.ManifestPath = filepath.Join(storage.Work, "manifest.json")
	storage.ActivePath = filepath.Join(storage.Work, "active")
	storage.liveDirectory = filepath.Join(storage.Work, "live")
	state := filepath.Join(root, "state")
	workspace := filepath.Join(root, "workspace", "disk.img")
	storage.configSourceDirectory = filepath.Join(root, "config")
	storage.launchSourceDirectory = filepath.Join(root, "launch")
	storage.trustSourceDirectory = filepath.Join(root, "trust")
	storage.credentialDirectory = filepath.Join(root, "credentials")
	storage.credentialTempDir = filepath.Join(root, "tmp")
	if err := os.MkdirAll(storage.liveDirectory, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(state, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(workspace), 0o700); err != nil {
		t.Fatal(err)
	}
	storage.Paths = map[string]string{
		"kernel":      filepath.Join(storage.liveDirectory, "vmlinux"),
		"rootfs":      filepath.Join(storage.liveDirectory, "rootfs.ext4"),
		"runner":      filepath.Join(storage.liveDirectory, "runner.img"),
		"state":       filepath.Join(state, "disk.img"),
		"workspace":   workspace,
		"config":      filepath.Join(storage.liveDirectory, "config.iso"),
		"launch":      filepath.Join(storage.liveDirectory, "launch.iso"),
		"trust":       filepath.Join(storage.liveDirectory, "trust.iso"),
		"credentials": filepath.Join(storage.liveDirectory, "credentials.ext4"),
	}
	for name, path := range storage.Paths {
		if err := os.WriteFile(path, []byte(name), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return storage
}

func TestCheckpointRestoresMatchingMemoryAndDisks(t *testing.T) {
	ctx := context.Background()
	storage := testStorage(t)
	memory := filepath.Join(t.TempDir(), "memory")
	vmstate := filepath.Join(t.TempDir(), "vmstate")
	if err := os.WriteFile(memory, []byte("RAM at suspended boundary"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(vmstate, []byte("VMM at suspended boundary"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := storage.MarkActive(); err != nil {
		t.Fatal(err)
	}
	manifest, err := storage.Checkpoint(ctx, memory, vmstate, "boot-test")
	if err != nil {
		t.Fatal(err)
	}
	if err := storage.ClearActive(); err != nil {
		t.Fatal(err)
	}
	unchanged, err := os.Stat(storage.Paths["runner"])
	if err != nil {
		t.Fatal(err)
	}
	// Equal-sized stale contents must not be mistaken for the checkpoint.
	if err := os.WriteFile(storage.Paths["rootfs"], []byte("stale!"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(storage.Paths["config"]); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(storage.Paths["state"], []byte("later state"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(storage.Paths["workspace"], []byte("later workspace"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := storage.MarkActive(); err != nil {
		t.Fatal(err)
	}
	loaded, err := storage.ReadManifest()
	if err != nil || loaded == nil {
		t.Fatalf("read checkpoint: manifest=%+v error=%v", loaded, err)
	}
	if err := storage.Restore(ctx, loaded); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"rootfs": "rootfs", "config": "config", "state": "state", "workspace": "workspace"} {
		data, err := os.ReadFile(storage.Paths[name])
		if err != nil || string(data) != want {
			t.Fatalf("restored %s = %q, %v", name, data, err)
		}
	}
	reused, err := os.Stat(storage.Paths["runner"])
	if err != nil || !os.SameFile(unchanged, reused) {
		t.Fatalf("matching live runner disk was replaced: %v", err)
	}
	generation := filepath.Join(storage.Work, manifest.Generation)
	for name, want := range map[string]string{"memory": "RAM at suspended boundary", "vmstate": "VMM at suspended boundary"} {
		data, err := os.ReadFile(filepath.Join(generation, name))
		if err != nil || string(data) != want {
			t.Fatalf("checkpoint %s = %q, %v", name, data, err)
		}
	}
	if err := storage.MarkActive(); err == nil || !strings.Contains(err.Error(), "uncertain") {
		t.Fatalf("active continuation was not fenced: %v", err)
	}
	if err := storage.ClearActive(); err != nil {
		t.Fatal(err)
	}
	second, err := storage.Checkpoint(ctx, memory, vmstate, "boot-test")
	if err != nil {
		t.Fatal(err)
	}
	capture := filepath.Join(storage.Work, ".capture")
	if err := os.Mkdir(capture, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := storage.FinishCheckpoint(second, capture); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(generation); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("previous checkpoint remains: %v", err)
	}
	if _, err := os.Stat(capture); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("capture remains: %v", err)
	}
	final, err := storage.ReadManifest()
	if err != nil || !reflect.DeepEqual(final, second) {
		t.Fatalf("final manifest = %+v, %v; want %+v", final, err, second)
	}
}

func TestPartialCheckpointCannotReplaceCommittedManifest(t *testing.T) {
	storage := testStorage(t)
	memory := filepath.Join(t.TempDir(), "memory")
	vmstate := filepath.Join(t.TempDir(), "missing-vmstate")
	if err := os.WriteFile(memory, []byte("ram"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(vmstate, []byte("state"), 0o600); err != nil {
		t.Fatal(err)
	}
	first, err := storage.Checkpoint(context.Background(), memory, vmstate, "boot-test")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(vmstate); err != nil {
		t.Fatal(err)
	}
	if _, err := storage.Checkpoint(context.Background(), memory, vmstate, "boot-test"); err == nil {
		t.Fatal("checkpoint with a missing VM state file succeeded")
	}
	loaded, err := storage.ReadManifest()
	if err != nil || !reflect.DeepEqual(loaded, first) {
		t.Fatalf("partial checkpoint replaced the committed manifest: %+v, %v", loaded, err)
	}
}

func TestRestoreRejectsIncompleteFileSetBeforeWriting(t *testing.T) {
	storage := testStorage(t)
	memory := filepath.Join(t.TempDir(), "memory")
	vmstate := filepath.Join(t.TempDir(), "vmstate")
	_ = os.WriteFile(memory, []byte("ram"), 0o600)
	_ = os.WriteFile(vmstate, []byte("vmstate"), 0o600)
	manifest, err := storage.Checkpoint(context.Background(), memory, vmstate, "boot-test")
	if err != nil {
		t.Fatal(err)
	}
	delete(manifest.Files, "workspace")
	if err := os.WriteFile(storage.Paths["state"], []byte("current state"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := storage.Restore(context.Background(), manifest); err == nil {
		t.Fatal("incomplete manifest file set was accepted")
	}
	data, err := os.ReadFile(storage.Paths["state"])
	if err != nil || string(data) != "current state" {
		t.Fatalf("restore wrote before validating the complete file set: %q, %v", data, err)
	}
}

func TestReadManifestBindsIdentityBinaryAndBootID(t *testing.T) {
	storage := testStorage(t)
	memory := filepath.Join(t.TempDir(), "memory")
	vmstate := filepath.Join(t.TempDir(), "vmstate")
	_ = os.WriteFile(memory, []byte("ram"), 0o600)
	_ = os.WriteFile(vmstate, []byte("vmstate"), 0o600)
	if _, err := storage.Checkpoint(context.Background(), memory, vmstate, "boot-test"); err != nil {
		t.Fatal(err)
	}
	originalConfig := storage.config
	storage.config.PodUID = "pod-other"
	if _, err := storage.ReadManifest(); err == nil || !strings.Contains(err.Error(), "different Pod") {
		t.Fatalf("checkpoint identity was not checked: %v", err)
	}
	storage.config = originalConfig
	if err := os.WriteFile(storage.firecracker, []byte("changed VMM"), 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := storage.ReadManifest(); err == nil || !strings.Contains(err.Error(), "configuration or Firecracker") {
		t.Fatalf("VMM digest was not checked: %v", err)
	}
	if err := os.Remove(storage.firecracker); err != nil {
		t.Fatal(err)
	}
	if _, err := storage.ReadManifest(); err == nil {
		t.Fatal("missing Firecracker binary was accepted")
	}
}

func TestReadManifestRejectsEmptyBootIDAndUnsafeGeneration(t *testing.T) {
	storage := testStorage(t)
	for _, manifest := range []*Manifest{
		{PodUID: storage.config.PodUID, SandboxUID: storage.config.SandboxUID, ConfigSHA256: storage.configDigest(), FirecrackerSHA256: mustDigest(t, storage.firecracker), Generation: "checkpoint-safe"},
		{PodUID: storage.config.PodUID, SandboxUID: storage.config.SandboxUID, BootID: "boot-test", ConfigSHA256: storage.configDigest(), FirecrackerSHA256: mustDigest(t, storage.firecracker), Generation: ".."},
	} {
		data, err := json.Marshal(manifest)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(storage.ManifestPath, data, 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := storage.ReadManifest(); err == nil {
			t.Fatalf("unsafe checkpoint accepted: %+v", manifest)
		}
	}
}

func TestReadManifestRejectsPreviousUnversionedConfigDigest(t *testing.T) {
	storage := testStorage(t)
	data, err := json.Marshal(storage.config)
	if err != nil {
		t.Fatal(err)
	}
	oldDigest := sha256.Sum256(data)
	manifest := Manifest{
		PodUID: storage.config.PodUID, SandboxUID: storage.config.SandboxUID, BootID: "boot-test",
		ConfigSHA256: hex.EncodeToString(oldDigest[:]), FirecrackerSHA256: mustDigest(t, storage.firecracker),
		Generation: "checkpoint-old", Files: map[string]FileRecord{},
	}
	encoded, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(storage.ManifestPath, encoded, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := storage.ReadManifest(); err == nil {
		t.Fatal("old checkpoint config digest was accepted")
	}
}

func mustDigest(t *testing.T, path string) string {
	t.Helper()
	record, err := digest(path)
	if err != nil {
		t.Fatal(err)
	}
	return record.SHA256
}

type fakeCommandError int

func (err fakeCommandError) Error() string { return fmt.Sprintf("exit %d", int(err)) }
func (err fakeCommandError) ExitCode() int { return int(err) }

func TestPrepareWorkspaceBlockPreservesExt4(t *testing.T) {
	device := filepath.Join(t.TempDir(), "workspace")
	if err := os.WriteFile(device, []byte("existing filesystem data"), 0o600); err != nil {
		t.Fatal(err)
	}
	var commands []string
	run := func(_ context.Context, name string, _ ...string) ([]byte, error) {
		commands = append(commands, name)
		return []byte("ext4\n"), nil
	}
	if err := prepareWorkspaceBlockWith(context.Background(), device, func(string) (bool, error) { return true, nil }, run); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(device)
	if err != nil || string(data) != "existing filesystem data" || !reflect.DeepEqual(commands, []string{"blkid"}) {
		t.Fatalf("existing ext4 workspace was changed: data=%q commands=%v error=%v", data, commands, err)
	}
}

func TestPrepareWorkspaceBlockFormatsOnlyUnsignedZeroDevice(t *testing.T) {
	device := filepath.Join(t.TempDir(), "workspace")
	if err := os.WriteFile(device, make([]byte, 8192), 0o600); err != nil {
		t.Fatal(err)
	}
	var commands []string
	run := func(_ context.Context, name string, _ ...string) ([]byte, error) {
		commands = append(commands, name)
		if name == "blkid" && len(commands) == 1 {
			return nil, fakeCommandError(2)
		}
		if name == "mkfs.ext4" {
			return nil, os.WriteFile(device, []byte("formatted ext4"), 0o600)
		}
		return []byte("ext4\n"), nil
	}
	if err := prepareWorkspaceBlockWith(context.Background(), device, func(string) (bool, error) { return true, nil }, run); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(device)
	if err != nil || string(data) != "formatted ext4" || !reflect.DeepEqual(commands, []string{"blkid", "mkfs.ext4", "blkid"}) {
		t.Fatalf("zero workspace was not formatted and verified: data=%q commands=%v error=%v", data, commands, err)
	}
}

func TestPrepareWorkspaceBlockRefusesDataAndNonExt4Signature(t *testing.T) {
	for _, test := range []struct {
		name string
		data []byte
		kind string
	}{
		{name: "unsigned nonzero", data: []byte("owner data")},
		{name: "other filesystem", data: []byte("xfs data"), kind: "xfs"},
	} {
		t.Run(test.name, func(t *testing.T) {
			device := filepath.Join(t.TempDir(), "workspace")
			if err := os.WriteFile(device, test.data, 0o600); err != nil {
				t.Fatal(err)
			}
			var commands []string
			run := func(_ context.Context, name string, _ ...string) ([]byte, error) {
				commands = append(commands, name)
				if test.kind != "" {
					return []byte(test.kind + "\n"), nil
				}
				return nil, fakeCommandError(2)
			}
			err := prepareWorkspaceBlockWith(context.Background(), device, func(string) (bool, error) { return true, nil }, run)
			if err == nil || !reflect.DeepEqual(commands, []string{"blkid"}) {
				t.Fatalf("unsafe workspace data was modified: err=%v commands=%v", err, commands)
			}
			data, readErr := os.ReadFile(device)
			if readErr != nil || !reflect.DeepEqual(data, test.data) {
				t.Fatalf("workspace contents changed: %q, %v", data, readErr)
			}
		})
	}
}

func TestLockOwnerExcludesConcurrentLauncher(t *testing.T) {
	storage := testStorage(t)
	first, err := storage.LockOwner()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := storage.LockOwner(); err == nil {
		t.Fatal("second launcher acquired the owner lock")
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	second, err := storage.LockOwner()
	if err != nil {
		t.Fatal(err)
	}
	if err := second.Close(); err != nil {
		t.Fatal(err)
	}
}
