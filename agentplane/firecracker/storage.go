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
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/google/uuid"
)

const (
	stateDirectory        = "/var/lib/agentplane/state"
	workspaceImage        = "/var/lib/agentplane/workspace/disk.img"
	workspaceBlock        = "/dev/agentplane-workspace"
	credentialDirectory   = "/run/agentplane/environment-runtime-tls"
	configSourceDirectory = "/run/agentplane/config"
	launchSourceDirectory = "/run/agentplane/sandbox"
	trustSourceDirectory  = "/run/agentplane/trust"
	configDigestDomain    = "agentplane-firecracker-config/v3\x00"
)

type FileRecord struct {
	Size int64 `json:"size"`
}

type Manifest struct {
	PodUID            string                `json:"pod_uid"`
	SandboxUID        string                `json:"sandbox_uid"`
	BootID            string                `json:"boot_id"`
	ConfigSHA256      string                `json:"config_sha256"`
	FirecrackerSHA256 string                `json:"firecracker_sha256"`
	Generation        string                `json:"generation"`
	Files             map[string]FileRecord `json:"files"`
}

type Storage struct {
	config       Config
	firecracker  string
	Work         string
	Paths        map[string]string
	ManifestPath string
	ActivePath   string

	liveDirectory         string
	configSourceDirectory string
	launchSourceDirectory string
	trustSourceDirectory  string
}

func NewStorage(config Config, binary string) *Storage {
	work := filepath.Join(stateDirectory, "firecracker")
	workspace := workspaceImage
	if config.WorkspaceKind == WorkspaceKindBlockFilesystem {
		workspace = workspaceBlock
	}
	live := filepath.Join(work, "live")
	storage := &Storage{
		config:                config,
		firecracker:           binary,
		Work:                  work,
		ManifestPath:          filepath.Join(work, "manifest.json"),
		ActivePath:            filepath.Join(work, "active"),
		liveDirectory:         live,
		configSourceDirectory: configSourceDirectory,
		launchSourceDirectory: launchSourceDirectory,
		trustSourceDirectory:  trustSourceDirectory,
		Paths: map[string]string{
			"kernel":    filepath.Join(live, "vmlinux"),
			"rootfs":    filepath.Join(live, "rootfs.ext4"),
			"runner":    filepath.Join(live, "runner.img"),
			"state":     filepath.Join(stateDirectory, "disk.img"),
			"workspace": workspace,
			"config":    filepath.Join(live, "config.iso"),
			"launch":    filepath.Join(live, "launch.iso"),
			"trust":     filepath.Join(live, "trust.iso"),
		},
	}
	return storage
}

func (storage *Storage) LockOwner() (io.Closer, error) {
	if err := os.MkdirAll(storage.Work, 0o700); err != nil {
		return nil, err
	}
	path := filepath.Join(storage.Work, "owner.lock")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		return nil, err
	}
	return &ownerLock{file: file}, nil
}

type ownerLock struct {
	file *os.File
}

func (lock *ownerLock) Close() error {
	unlockErr := syscall.Flock(int(lock.file.Fd()), syscall.LOCK_UN)
	closeErr := lock.file.Close()
	return errors.Join(unlockErr, closeErr)
}

func (storage *Storage) Prepare(ctx context.Context) error {
	if err := storage.selectInitrd(); err != nil {
		return err
	}
	for _, path := range []string{storage.ManifestPath, storage.ActivePath} {
		if _, err := os.Stat(path); err == nil {
			return nil
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	if err := os.MkdirAll(storage.liveDirectory, 0o700); err != nil {
		return err
	}
	for _, image := range []struct {
		name   string
		source string
	}{
		{name: "kernel", source: filepath.Join(storage.config.BootDirectory, "vmlinux")},
		{name: "rootfs", source: filepath.Join(storage.config.BootDirectory, "rootfs.ext4")},
		{name: "runner", source: filepath.Join(storage.config.RunnerDirectory, "disk", "runner.img")},
		{name: "initrd", source: filepath.Join(storage.config.BootDirectory, "initramfs.cpio")},
	} {
		destination, present := storage.Paths[image.name]
		if !present {
			continue
		}
		if _, err := os.Stat(destination); err == nil {
			continue
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		staging := uniqueSibling(destination)
		if err := copyAndSync(ctx, image.source, staging); err != nil {
			_ = os.Remove(staging)
			return fmt.Errorf("copy %s image: %w", image.name, err)
		}
		if err := os.Rename(staging, destination); err != nil {
			_ = os.Remove(staging)
			return err
		}
		if err := syncDirectory(storage.liveDirectory); err != nil {
			return err
		}
	}
	if err := createExt4(ctx, storage.Paths["state"], fmt.Sprintf("%dM", storage.config.StateSizeMiB), "APSTATE", ""); err != nil {
		return err
	}
	if storage.config.WorkspaceKind == WorkspaceKindRawDiskImage {
		if err := os.MkdirAll(filepath.Dir(storage.Paths["workspace"]), 0o700); err != nil {
			return err
		}
		if err := createExt4(ctx, storage.Paths["workspace"], fmt.Sprintf("%dM", storage.config.WorkspaceSizeMiB), "APWORKSPACE", ""); err != nil {
			return err
		}
	} else if err := prepareWorkspaceBlock(ctx, storage.Paths["workspace"]); err != nil {
		return err
	}
	for _, image := range []struct {
		name   string
		label  string
		source string
	}{
		{name: "config", label: "APCONFIG", source: storage.configSourceDirectory},
		{name: "launch", label: "APLAUNCH", source: storage.launchSourceDirectory},
		{name: "trust", label: "APTRUST", source: storage.trustSourceDirectory},
	} {
		if err := createISO(ctx, storage.Paths[image.name], image.label, image.source); err != nil {
			return err
		}
	}
	return nil
}

// Boot images may supply an initramfs for kernels with modular boot drivers.
// Include it in the same checkpoint file set on cold boot and launcher restart.
func (storage *Storage) selectInitrd() error {
	path := filepath.Join(storage.config.BootDirectory, "initramfs.cpio")
	info, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		delete(storage.Paths, "initrd")
		return nil
	}
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("boot initramfs must be a regular file: %s", path)
	}
	storage.Paths["initrd"] = filepath.Join(storage.liveDirectory, "initramfs.cpio")
	return nil
}

func (storage *Storage) ReadManifest() (*Manifest, error) {
	data, err := os.ReadFile(storage.ManifestPath)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.DisallowUnknownFields()
	var manifest Manifest
	if err := decoder.Decode(&manifest); err != nil {
		return nil, fmt.Errorf("decode checkpoint manifest: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("checkpoint manifest contains trailing JSON")
		}
		return nil, fmt.Errorf("decode checkpoint manifest trailer: %w", err)
	}
	if manifest.PodUID != storage.config.PodUID || manifest.SandboxUID != storage.config.SandboxUID {
		return nil, fmt.Errorf("checkpoint belongs to a different Pod or Sandbox")
	}
	if manifest.BootID == "" {
		return nil, fmt.Errorf("checkpoint has no boot ID")
	}
	binaryDigest, err := digest(storage.firecracker)
	if err != nil {
		return nil, err
	}
	if manifest.ConfigSHA256 != storage.configDigest() || manifest.FirecrackerSHA256 != binaryDigest {
		return nil, fmt.Errorf("checkpoint configuration or Firecracker binary changed")
	}
	if !validGeneration(manifest.Generation) {
		return nil, fmt.Errorf("invalid checkpoint generation")
	}
	return &manifest, nil
}

func (storage *Storage) MarkActive() error {
	marker, err := os.OpenFile(storage.ActivePath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if errors.Is(err, os.ErrExist) {
		return fmt.Errorf("an active VM continuation has an uncertain outcome")
	}
	if err != nil {
		return err
	}
	_, writeErr := marker.WriteString(storage.config.PodUID)
	syncErr := marker.Sync()
	closeErr := marker.Close()
	if err := errors.Join(writeErr, syncErr, closeErr); err != nil {
		return err
	}
	return syncDirectory(storage.Work)
}

func (storage *Storage) ClearActive() error {
	if err := os.Remove(storage.ActivePath); err != nil {
		return err
	}
	return syncDirectory(storage.Work)
}

func (storage *Storage) Restore(ctx context.Context, manifest *Manifest) error {
	if err := storage.selectInitrd(); err != nil {
		return err
	}
	if manifest == nil || !validGeneration(manifest.Generation) {
		return fmt.Errorf("invalid checkpoint generation")
	}
	expected := make(map[string]struct{}, len(storage.Paths)+2)
	for name := range storage.Paths {
		expected[name] = struct{}{}
	}
	expected["memory"] = struct{}{}
	expected["vmstate"] = struct{}{}
	if len(manifest.Files) != len(expected) {
		return fmt.Errorf("checkpoint disk set is incomplete")
	}
	for name := range expected {
		if _, ok := manifest.Files[name]; !ok {
			return fmt.Errorf("checkpoint disk set is incomplete")
		}
	}
	generation := filepath.Join(storage.Work, manifest.Generation)
	for name, record := range manifest.Files {
		if record.Size < 0 {
			return fmt.Errorf("checkpoint file has negative size: %s", name)
		}
		info, err := os.Lstat(filepath.Join(generation, name))
		if err != nil {
			return fmt.Errorf("inspect checkpoint file %s: %w", name, err)
		}
		if !info.Mode().IsRegular() || info.Size() != record.Size {
			return fmt.Errorf("checkpoint file has invalid type or size: %s", name)
		}
	}
	for name, target := range storage.Paths {
		source := filepath.Join(generation, name)
		block, err := isBlockDevice(target)
		if err != nil {
			return err
		}
		if block {
			if err := copyToBlockDevice(ctx, source, target); err != nil {
				return err
			}
			continue
		}
		staging := uniqueSibling(target)
		if err := copyAndSync(ctx, source, staging); err != nil {
			_ = os.Remove(staging)
			return err
		}
		if err := os.Rename(staging, target); err != nil {
			_ = os.Remove(staging)
			return err
		}
		if err := syncDirectory(filepath.Dir(target)); err != nil {
			return err
		}
	}
	return nil
}

// Checkpoint commits a generation of the live disks plus the captured RAM and VM state. It takes ownership
// of the `memory` and `vmstate` files: they are renamed into the generation, so both must be on
// the filesystem holding `storage.Work`. Live disks are copied, because the VM keeps writing them.
func (storage *Storage) Checkpoint(ctx context.Context, memory, vmstate, bootID string) (*Manifest, error) {
	if bootID == "" {
		return nil, fmt.Errorf("checkpoint boot ID must be non-empty")
	}
	generation := randomName("checkpoint-")
	directory := filepath.Join(storage.Work, generation)
	if err := os.Mkdir(directory, 0o700); err != nil {
		return nil, err
	}
	files := make(map[string]FileRecord, len(storage.Paths)+2)
	for name, source := range storage.Paths {
		record, err := copyDurable(ctx, source, filepath.Join(directory, name))
		if err != nil {
			return nil, fmt.Errorf("checkpoint %s disk: %w", name, err)
		}
		files[name] = record
	}
	for name, source := range map[string]string{"memory": memory, "vmstate": vmstate} {
		record, err := moveDurable(source, filepath.Join(directory, name))
		if err != nil {
			return nil, fmt.Errorf("checkpoint %s: %w", name, err)
		}
		files[name] = record
	}
	if err := syncDirectory(directory); err != nil {
		return nil, err
	}
	binaryDigest, err := digest(storage.firecracker)
	if err != nil {
		return nil, err
	}
	manifest := &Manifest{
		PodUID:            storage.config.PodUID,
		SandboxUID:        storage.config.SandboxUID,
		BootID:            bootID,
		ConfigSHA256:      storage.configDigest(),
		FirecrackerSHA256: binaryDigest,
		Generation:        generation,
		Files:             files,
	}
	data, err := json.Marshal(manifest)
	if err != nil {
		return nil, err
	}
	staging := randomName(".manifest-")
	stagingPath := filepath.Join(storage.Work, staging)
	output, err := os.OpenFile(stagingPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	_, writeErr := output.Write(data)
	syncErr := output.Sync()
	closeErr := output.Close()
	if err := errors.Join(writeErr, syncErr, closeErr); err != nil {
		_ = os.Remove(stagingPath)
		return nil, err
	}
	if err := os.Rename(stagingPath, storage.ManifestPath); err != nil {
		_ = os.Remove(stagingPath)
		return nil, err
	}
	if err := syncDirectory(storage.Work); err != nil {
		return nil, err
	}
	return manifest, nil
}

func (storage *Storage) FinishCheckpoint(manifest *Manifest, capture string) error {
	if manifest == nil || !validGeneration(manifest.Generation) {
		return fmt.Errorf("invalid checkpoint generation")
	}
	if err := os.RemoveAll(capture); err != nil {
		return err
	}
	entries, err := os.ReadDir(storage.Work)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), "checkpoint-") && entry.Name() != manifest.Generation {
			if err := os.RemoveAll(filepath.Join(storage.Work, entry.Name())); err != nil {
				return err
			}
		}
	}
	return syncDirectory(storage.Work)
}

func (storage *Storage) configDigest() string {
	data, _ := json.Marshal(storage.config)
	hash := sha256.New()
	_, _ = io.WriteString(hash, configDigestDomain)
	_, _ = hash.Write(data)
	return hex.EncodeToString(hash.Sum(nil))
}

func validGeneration(generation string) bool {
	return generation != "" && generation != "." && generation != ".." &&
		filepath.Base(generation) == generation && strings.HasPrefix(generation, "checkpoint-")
}

// The Firecracker binary is small and its exact contents determine snapshot compatibility.
func digest(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	hash := sha256.New()
	_, copyErr := io.Copy(hash, file)
	closeErr := file.Close()
	if err := errors.Join(copyErr, closeErr); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

// moveDurable renames a finished capture into the generation after flushing its contents.
func moveDurable(source, destination string) (FileRecord, error) {
	file, err := os.Open(source)
	if err != nil {
		return FileRecord{}, err
	}
	syncErr := file.Sync()
	closeErr := file.Close()
	if err := errors.Join(syncErr, closeErr); err != nil {
		return FileRecord{}, err
	}
	if err := os.Chmod(source, 0o600); err != nil {
		return FileRecord{}, err
	}
	if err := os.Rename(source, destination); err != nil {
		return FileRecord{}, err
	}
	info, err := os.Lstat(destination)
	if err != nil {
		return FileRecord{}, err
	}
	if !info.Mode().IsRegular() {
		return FileRecord{}, fmt.Errorf("checkpoint capture is not a regular file: %s", destination)
	}
	return FileRecord{Size: info.Size()}, nil
}

func copyDurable(ctx context.Context, source, destination string) (FileRecord, error) {
	if err := copyAndSync(ctx, source, destination); err != nil {
		return FileRecord{}, err
	}
	info, err := os.Lstat(destination)
	if err != nil {
		return FileRecord{}, err
	}
	if !info.Mode().IsRegular() {
		return FileRecord{}, fmt.Errorf("checkpoint copy is not a regular file: %s", destination)
	}
	return FileRecord{Size: info.Size()}, nil
}

func copyAndSync(ctx context.Context, source, destination string) error {
	block, err := isBlockDevice(source)
	if err != nil {
		return err
	}
	if block {
		if err := copySparse(ctx, source, destination); err != nil {
			return err
		}
	} else {
		if _, err := runCommand(ctx, "cp", "--reflink=auto", "--sparse=always", "--", source, destination); err != nil {
			return err
		}
		if err := os.Chmod(destination, 0o600); err != nil {
			return err
		}
		file, err := os.Open(destination)
		if err != nil {
			return err
		}
		syncErr := file.Sync()
		closeErr := file.Close()
		if err := errors.Join(syncErr, closeErr); err != nil {
			return err
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return nil
}

func copySparse(ctx context.Context, source, destination string) error {
	reader, err := os.Open(source)
	if err != nil {
		return err
	}
	writer, err := os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		_ = reader.Close()
		return err
	}
	buffer := make([]byte, 1024*1024)
	var size int64
	for {
		if err := ctx.Err(); err != nil {
			_ = reader.Close()
			_ = writer.Close()
			return err
		}
		count, readErr := reader.Read(buffer)
		if count > 0 {
			chunk := buffer[:count]
			if !allZero(chunk) {
				written, err := writer.Write(chunk)
				if err != nil {
					_ = reader.Close()
					_ = writer.Close()
					return err
				}
				if written != count {
					_ = reader.Close()
					_ = writer.Close()
					return io.ErrShortWrite
				}
			} else if _, err := writer.Seek(int64(count), io.SeekCurrent); err != nil {
				_ = reader.Close()
				_ = writer.Close()
				return err
			}
			size += int64(count)
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			_ = reader.Close()
			_ = writer.Close()
			return readErr
		}
	}
	if err := writer.Truncate(size); err != nil {
		_ = reader.Close()
		_ = writer.Close()
		return err
	}
	readCloseErr := reader.Close()
	syncErr := writer.Sync()
	closeErr := writer.Close()
	return errors.Join(readCloseErr, syncErr, closeErr)
}

func allZero(data []byte) bool {
	for _, value := range data {
		if value != 0 {
			return false
		}
	}
	return true
}

func createExt4(ctx context.Context, destination, size, label, source string) error {
	if _, err := os.Stat(destination); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	staging := uniqueSibling(destination)
	if _, err := runCommand(ctx, "truncate", "-s", size, staging); err != nil {
		return err
	}
	args := []string{"-F", "-q", "-m", "0", "-L", label}
	if source != "" {
		args = append(args, "-d", source)
	}
	args = append(args, staging)
	if _, err := runCommand(ctx, "mkfs.ext4", args...); err != nil {
		_ = os.Remove(staging)
		return err
	}
	if err := syncFile(staging); err != nil {
		_ = os.Remove(staging)
		return err
	}
	if err := os.Rename(staging, destination); err != nil {
		_ = os.Remove(staging)
		return err
	}
	return syncDirectory(filepath.Dir(destination))
}

func createISO(ctx context.Context, destination, label, source string) error {
	if _, err := os.Stat(destination); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	staging := uniqueSibling(destination)
	if _, err := runCommand(ctx, "xorriso", "-as", "mkisofs", "-quiet", "-R", "-J", "-V", label, "-o", staging, source); err != nil {
		_ = os.Remove(staging)
		return err
	}
	if err := syncFile(staging); err != nil {
		_ = os.Remove(staging)
		return err
	}
	if err := os.Rename(staging, destination); err != nil {
		_ = os.Remove(staging)
		return err
	}
	return syncDirectory(filepath.Dir(destination))
}

func prepareWorkspaceBlock(ctx context.Context, device string) error {
	return prepareWorkspaceBlockWith(ctx, device, isBlockDevice, runCommand)
}

type (
	commandRunner func(context.Context, string, ...string) ([]byte, error)
	blockDetector func(string) (bool, error)
)

func prepareWorkspaceBlockWith(ctx context.Context, device string, isBlock blockDetector, run commandRunner) error {
	block, err := isBlock(device)
	if err != nil {
		return err
	}
	if !block {
		return fmt.Errorf("workspace block device is missing: %s", device)
	}
	output, err := run(ctx, "blkid", "-p", "-s", "TYPE", "-o", "value", device)
	if err == nil {
		if strings.TrimSpace(string(output)) != "ext4" {
			return fmt.Errorf("workspace block device has a non-ext4 signature: %s", device)
		}
		return nil
	}
	if commandExitCode(err) != 2 {
		return fmt.Errorf("cannot inspect workspace block device %s: %w", device, err)
	}
	file, err := os.Open(device)
	if err != nil {
		return err
	}
	buffer := make([]byte, 4*1024*1024)
	for {
		if err := ctx.Err(); err != nil {
			_ = file.Close()
			return err
		}
		count, readErr := file.Read(buffer)
		if count > 0 && !allZero(buffer[:count]) {
			_ = file.Close()
			return fmt.Errorf("workspace block device has unsigned nonzero data: %s", device)
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			_ = file.Close()
			return readErr
		}
	}
	if err := file.Close(); err != nil {
		return err
	}
	if _, err := run(ctx, "mkfs.ext4", "-F", "-q", "-m", "0", "-L", "APWORKSPACE", device); err != nil {
		return err
	}
	if err := syncFile(device); err != nil {
		return err
	}
	formatted, err := run(ctx, "blkid", "-p", "-s", "TYPE", "-o", "value", device)
	if err != nil || strings.TrimSpace(string(formatted)) != "ext4" {
		return fmt.Errorf("workspace block format did not produce ext4: %s", device)
	}
	return nil
}

func copyToBlockDevice(ctx context.Context, source, destination string) error {
	reader, err := os.Open(source)
	if err != nil {
		return err
	}
	writer, err := os.OpenFile(destination, os.O_WRONLY, 0)
	if err != nil {
		_ = reader.Close()
		return err
	}
	buffer := make([]byte, 1024*1024)
	var copyErr error
	for {
		if err := ctx.Err(); err != nil {
			copyErr = err
			break
		}
		count, readErr := reader.Read(buffer)
		if count > 0 {
			written, err := writer.Write(buffer[:count])
			if err != nil {
				copyErr = err
				break
			}
			if written != count {
				copyErr = io.ErrShortWrite
				break
			}
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			copyErr = readErr
			break
		}
	}
	readCloseErr := reader.Close()
	syncErr := writer.Sync()
	writeCloseErr := writer.Close()
	if err := errors.Join(copyErr, readCloseErr, syncErr, writeCloseErr); err != nil {
		return err
	}
	return ctx.Err()
}

func isBlockDevice(path string) (bool, error) {
	info, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	mode := info.Mode()
	return mode&os.ModeDevice != 0 && mode&os.ModeCharDevice == 0, nil
}

func commandExitCode(err error) int {
	var exitError interface{ ExitCode() int }
	if errors.As(err, &exitError) {
		return exitError.ExitCode()
	}
	return -1
}

func runCommand(ctx context.Context, name string, args ...string) ([]byte, error) {
	command := exec.CommandContext(ctx, name, args...)
	output, err := command.CombinedOutput()
	if err != nil {
		return output, fmt.Errorf("%s failed: %w: %s", name, err, strings.TrimSpace(string(output)))
	}
	return output, nil
}

func syncFile(path string) error {
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	syncErr := file.Sync()
	closeErr := file.Close()
	return errors.Join(syncErr, closeErr)
}

func syncDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	syncErr := directory.Sync()
	closeErr := directory.Close()
	return errors.Join(syncErr, closeErr)
}

func uniqueSibling(path string) string {
	name := randomName("." + filepath.Base(path) + ".")
	return filepath.Join(filepath.Dir(path), name)
}

func randomName(prefix string) string {
	return prefix + uuid.NewString()
}
