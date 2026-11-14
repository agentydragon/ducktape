package kvm

import (
	"bufio"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

const (
	actionRootPath    = "/"
	sharedK3sDataPath = "/var/lib/k3s-env"
	privateBasePath   = "/var/lib/k3s-persistence-v7"
	privateMarkerPath = privateBasePath + "/server.json"
	privateTempPath   = privateBasePath + "/server.tmp"
	privateDataPath   = privateBasePath + "/data"
	containerdSocket  = "/run/k3s/containerd/containerd.sock"
	debugSocket       = "/run/k3s/containerd/debug.sock"
)

type actionMount struct {
	id          int
	mountPoint  string
	filesystem  string
	propagation []string
	raw         string
}

// TestActionK3sIsolationPreflight inspects the Docker action's effective mount and namespace
// context before any K3s test is run. It deliberately does not open /dev/kvm or mutate host state.
func TestActionK3sIsolationPreflight(t *testing.T) {
	report, failures := collectActionIsolationEvidence()
	path, err := writeActionIsolationEvidence(report)
	if err != nil {
		t.Fatalf("write action-isolation diagnostics: %v", err)
	}
	if len(failures) != 0 {
		t.Fatalf("action-isolation preflight failed (%s); diagnostics: %s", strings.Join(failures, ", "), path)
	}
}

func collectActionIsolationEvidence() (string, []string) {
	var report strings.Builder
	var failures []string
	report.WriteString("Read-only Docker action isolation preflight. This does not prove isolation between hostile jobs or from host processes.\n")
	report.WriteString("Namespace links are recorded as observations; PID 1 is not used as a host-separation proof.\n")

	mountBytes, mountErr := appendReadFile(&report, "/proc/self/mountinfo")
	var mounts []actionMount
	if mountErr != nil {
		failures = append(failures, "mountinfo-unreadable")
	} else {
		parsed, err := parseActionMounts(mountBytes)
		if err != nil {
			failures = append(failures, "mountinfo-invalid")
			report.WriteString("mountinfo parse error: " + err.Error() + "\n")
		} else {
			mounts = parsed
		}
	}

	if err := appendNamespaceLinks(&report); err != nil {
		failures = append(failures, "namespace-links-incomplete")
		report.WriteString("namespace inspection error: " + err.Error() + "\n")
	}
	cgroupBytes, cgroupErr := appendReadFile(&report, "/proc/self/cgroup")
	if cgroupErr != nil {
		failures = append(failures, "cgroup-unreadable")
	} else if strings.TrimSpace(string(cgroupBytes)) != "0::/" {
		failures = append(failures, "unexpected-cgroup-path")
	}

	if info, err := os.Lstat("/.dockerenv"); err != nil {
		failures = append(failures, "docker-marker-missing")
		report.WriteString("/.dockerenv: unavailable: " + err.Error() + "\n")
	} else {
		report.WriteString(fmt.Sprintf("/.dockerenv: mode=%s size=%d\n", info.Mode(), info.Size()))
		if !info.Mode().IsRegular() {
			failures = append(failures, "docker-marker-not-regular")
		}
	}

	privateRootConfirmed := false
	if len(mounts) != 0 {
		mountFailures, confirmed := checkActionMounts(&report, mounts)
		failures = append(failures, mountFailures...)
		privateRootConfirmed = confirmed
	}
	if privateRootConfirmed {
		failures = append(failures, checkFreshPrivateBaseline(&report)...)
	} else {
		report.WriteString("\nprivate baseline paths not inspected because their action-private mount was not confirmed\n")
		failures = append(failures, "private-baseline-not-inspected")
	}
	sort.Strings(failures)
	return report.String(), failures
}

func appendReadFile(report *strings.Builder, path string) ([]byte, error) {
	data, err := os.ReadFile(path)
	report.WriteString("\n--- " + path + " ---\n")
	if err != nil {
		report.WriteString("read error: " + err.Error() + "\n")
		return nil, err
	}
	report.Write(data)
	if len(data) == 0 || data[len(data)-1] != '\n' {
		report.WriteByte('\n')
	}
	return data, nil
}

func appendNamespaceLinks(report *strings.Builder) error {
	const directory = "/proc/self/ns"
	report.WriteString("\n--- namespace symlinks ---\n")
	entries, err := os.ReadDir(directory)
	if err != nil {
		return err
	}
	seen := make(map[string]bool, len(entries))
	var failures []string
	for _, entry := range entries {
		name := entry.Name()
		seen[name] = true
		link, err := os.Readlink(filepath.Join(directory, name))
		if err != nil {
			report.WriteString(name + ": readlink error: " + err.Error() + "\n")
			failures = append(failures, name)
			continue
		}
		report.WriteString(name + ": " + link + "\n")
	}
	for _, required := range []string{"cgroup", "ipc", "mnt", "net", "pid", "user", "uts"} {
		if !seen[required] {
			failures = append(failures, "missing-"+required)
		}
	}
	if len(failures) != 0 {
		return fmt.Errorf("unobserved namespace links: %s", strings.Join(failures, ", "))
	}
	return nil
}

func parseActionMounts(data []byte) ([]actionMount, error) {
	var mounts []actionMount
	scanner := bufio.NewScanner(strings.NewReader(string(data)))
	scanner.Buffer(make([]byte, 4096), 4*1024*1024)
	for lineNumber := 1; scanner.Scan(); lineNumber++ {
		line := scanner.Text()
		fields := strings.Fields(line)
		separator := -1
		for i, field := range fields {
			if field == "-" {
				separator = i
				break
			}
		}
		if separator < 6 || separator+3 >= len(fields) {
			return nil, fmt.Errorf("line %d has malformed fields", lineNumber)
		}
		id, err := strconv.Atoi(fields[0])
		if err != nil {
			return nil, fmt.Errorf("line %d has invalid mount ID", lineNumber)
		}
		mounts = append(mounts, actionMount{
			id:          id,
			mountPoint:  fields[4],
			filesystem:  fields[separator+1],
			propagation: append([]string(nil), fields[6:separator]...),
			raw:         line,
		})
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if len(mounts) == 0 {
		return nil, errors.New("no mount entries")
	}
	return mounts, nil
}

func checkActionMounts(report *strings.Builder, mounts []actionMount) ([]string, bool) {
	var failures []string
	var roots []actionMount
	for _, mount := range mounts {
		if mount.mountPoint == actionRootPath {
			roots = append(roots, mount)
		}
	}
	if len(roots) != 1 {
		failures = append(failures, "root-mount-ambiguous")
		appendMountMetadata(report, "configured shared K3s path (contents not opened)", sharedK3sDataPath, mounts)
		return failures, false
	}
	root := roots[0]
	report.WriteString(fmt.Sprintf("\nroot mount ID %d: %s\n", root.id, root.raw))
	rootIsPrivateOverlay := root.filesystem == "overlay"
	if root.filesystem != "overlay" {
		failures = append(failures, "root-filesystem-not-overlay")
		rootIsPrivateOverlay = false
	}
	for _, option := range root.propagation {
		if strings.HasPrefix(option, "shared:") || strings.HasPrefix(option, "master:") {
			failures = append(failures, "root-mount-propagates")
			rootIsPrivateOverlay = false
		}
	}
	privateRootConfirmed := false
	confirmedSocketPaths := make(map[string]bool, 2)
	for _, target := range criticalActionPaths {
		appendMountMetadata(report, "critical runtime path", target, mounts)
		if err := checkCriticalPathMount(target, root, mounts); err != nil {
			failures = append(failures, err.Error())
			continue
		}
		if !rootIsPrivateOverlay {
			continue
		}
		if err := checkPathComponents(report, target); err != nil {
			failures = append(failures, err.Error())
		} else if target == privateBasePath {
			privateRootConfirmed = true
		} else if target == containerdSocket || target == debugSocket {
			confirmedSocketPaths[target] = true
		}
	}
	failures = append(failures, checkFreshRuntimeSockets(report, confirmedSocketPaths)...)
	appendMountMetadata(report, "configured shared K3s path (contents not opened)", sharedK3sDataPath, mounts)
	return failures, privateRootConfirmed
}

var criticalActionPaths = []string{
	"/run/k3s",
	containerdSocket,
	debugSocket,
	"/var/lib/kubelet",
	"/var/lib/cni",
	"/var/log/pods",
	"/var/log/containers",
	"/run/flannel",
	"/run/netns",
	privateBasePath,
}

func checkCriticalPathMount(target string, root actionMount, mounts []actionMount) error {
	var covering []actionMount
	longest := -1
	for _, mount := range mounts {
		if !mountCoversPath(mount.mountPoint, target) {
			continue
		}
		if len(mount.mountPoint) > longest {
			covering = []actionMount{mount}
			longest = len(mount.mountPoint)
		} else if len(mount.mountPoint) == longest {
			covering = append(covering, mount)
		}
	}
	if len(covering) != 1 {
		return fmt.Errorf("%s-mount-ambiguous", strings.ReplaceAll(strings.Trim(target, "/"), "/", "-"))
	}
	if covering[0].id != root.id || covering[0].mountPoint != actionRootPath || covering[0].filesystem != "overlay" {
		return fmt.Errorf("%s-not-on-action-root", strings.ReplaceAll(strings.Trim(target, "/"), "/", "-"))
	}
	for _, mount := range mounts {
		if isStrictPathDescendant(target, mount.mountPoint) {
			return fmt.Errorf("%s-has-submount", strings.ReplaceAll(strings.Trim(target, "/"), "/", "-"))
		}
	}
	return nil
}

func mountCoversPath(mountPoint, target string) bool {
	if mountPoint == "/" {
		return strings.HasPrefix(target, "/")
	}
	return target == mountPoint || strings.HasPrefix(target, mountPoint+"/")
}

func isStrictPathDescendant(parent, candidate string) bool {
	return candidate != parent && mountCoversPath(parent, candidate)
}

func checkPathComponents(report *strings.Builder, path string) error {
	current := string(filepath.Separator)
	for _, component := range strings.Split(strings.Trim(path, string(filepath.Separator)), string(filepath.Separator)) {
		current = filepath.Join(current, component)
		info, err := os.Lstat(current)
		switch {
		case errors.Is(err, fs.ErrNotExist):
			report.WriteString(current + ": absent\n")
			return nil
		case err != nil:
			report.WriteString(current + ": lstat error: " + err.Error() + "\n")
			return fmt.Errorf("%s-path-unreadable", strings.ReplaceAll(strings.Trim(path, "/"), "/", "-"))
		case info.Mode()&os.ModeSymlink != 0:
			report.WriteString(fmt.Sprintf("%s: symlink mode=%s\n", current, info.Mode()))
			return fmt.Errorf("%s-path-has-symlink", strings.ReplaceAll(strings.Trim(path, "/"), "/", "-"))
		default:
			report.WriteString(fmt.Sprintf("%s: mode=%s\n", current, info.Mode()))
		}
	}
	return nil
}

func appendMountMetadata(report *strings.Builder, description, path string, mounts []actionMount) {
	report.WriteString("\n--- " + description + ": " + path + " ---\n")
	count := 0
	for _, mount := range mounts {
		if mountCoversPath(mount.mountPoint, path) || mountCoversPath(path, mount.mountPoint) {
			report.WriteString(mount.raw + "\n")
			count++
		}
	}
	if count == 0 {
		report.WriteString("no covering or nested mount entries\n")
	}
}

func checkFreshPrivateBaseline(report *strings.Builder) []string {
	var failures []string
	report.WriteString("\n--- fresh private baseline path metadata ---\n")
	info, err := os.Lstat(privateBasePath)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		report.WriteString(privateBasePath + ": absent\n")
	case err != nil:
		report.WriteString(privateBasePath + ": lstat error: " + err.Error() + "\n")
		failures = append(failures, "private-baseline-path-unreadable")
		return failures
	case info.Mode()&os.ModeSymlink != 0 || !info.IsDir():
		report.WriteString(fmt.Sprintf("%s: exists mode=%s size=%d\n", privateBasePath, info.Mode(), info.Size()))
		failures = append(failures, "private-baseline-root-not-directory")
		return failures
	default:
		report.WriteString(fmt.Sprintf("%s: exists mode=%s size=%d\n", privateBasePath, info.Mode(), info.Size()))
	}

	for _, path := range []string{privateMarkerPath, privateTempPath, privateDataPath} {
		info, err := os.Lstat(path)
		switch {
		case err == nil:
			report.WriteString(fmt.Sprintf("%s: exists mode=%s size=%d\n", path, info.Mode(), info.Size()))
			failures = append(failures, "private-baseline-state-exists")
		case errors.Is(err, fs.ErrNotExist):
			report.WriteString(path + ": absent\n")
		default:
			report.WriteString(path + ": lstat error: " + err.Error() + "\n")
			failures = append(failures, "private-baseline-path-unreadable")
		}
	}
	return failures
}

func checkFreshRuntimeSockets(report *strings.Builder, confirmed map[string]bool) []string {
	var failures []string
	report.WriteString("\n--- K3s runtime socket path metadata ---\n")
	for _, path := range []string{containerdSocket, debugSocket} {
		if !confirmed[path] {
			report.WriteString(path + ": not inspected because the path is not confirmed action-private\n")
			continue
		}
		info, err := os.Lstat(path)
		switch {
		case err == nil:
			report.WriteString(fmt.Sprintf("%s: exists mode=%s\n", path, info.Mode()))
			failures = append(failures, "k3s-runtime-socket-path-exists")
		case errors.Is(err, fs.ErrNotExist):
			report.WriteString(path + ": absent\n")
		default:
			report.WriteString(path + ": lstat error: " + err.Error() + "\n")
			failures = append(failures, "k3s-runtime-socket-path-unreadable")
		}
	}
	return failures
}

func writeActionIsolationEvidence(report string) (string, error) {
	directory := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if directory == "" || !filepath.IsAbs(directory) {
		return "", errors.New("TEST_UNDECLARED_OUTPUTS_DIR is unset or not absolute")
	}
	path := filepath.Join(directory, "kvm-action-isolation-preflight.txt")
	if err := os.WriteFile(path, []byte(report), 0o600); err != nil {
		return "", err
	}
	return path, nil
}
