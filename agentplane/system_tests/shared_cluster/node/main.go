package main

import (
	"errors"
	"fmt"
	"os"
	"path"
	"runtime"
	"strconv"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

const (
	cgroupMountpoint = "/sys/fs/cgroup"
	controlCgroup    = "control"
	// Kubelet's cgroup root must be configured as `/workloads` in the node config.
	workloadsCgroup = "workloads"
	maxNodePIDs     = uint64(8192)
)

var delegatedControllers = []string{"cpu", "memory", "pids"}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "shared K3s node setup: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	parentCgroup, mount, membership, err := readCurrentCgroup(
		"/proc/self/mountinfo",
		"/proc/self/cgroup",
		cgroupMountpoint,
	)
	if err != nil {
		return fmt.Errorf("locate container cgroup: %w", err)
	}
	if err := validateInitialCgroup(mount, membership); err != nil {
		return err
	}
	if err := validateCgroupBounds(parentCgroup); err != nil {
		return fmt.Errorf("validate container cgroup: %w", err)
	}

	runtime.LockOSThread()
	if err := unix.Unshare(unix.CLONE_NEWNS | unix.CLONE_NEWCGROUP); err != nil {
		return fmt.Errorf("unshare mount and cgroup namespaces: %w", err)
	}
	if err := unix.Mount("", "/", "", unix.MS_REC|unix.MS_PRIVATE, ""); err != nil {
		return fmt.Errorf("make node mount namespace private: %w", err)
	}
	if err := requireCgroupNamespaceRoot("/proc/self/cgroup"); err != nil {
		return err
	}
	if err := unix.Mount("cgroup2", cgroupMountpoint, "cgroup2", unix.MS_NOSUID|unix.MS_NODEV|unix.MS_NOEXEC, ""); err != nil {
		return fmt.Errorf("mount private cgroup v2 view: %w", err)
	}

	root, _, _, err := readCurrentCgroup(
		"/proc/self/mountinfo",
		"/proc/self/cgroup",
		cgroupMountpoint,
	)
	if err != nil {
		return fmt.Errorf("verify private cgroup mount: %w", err)
	}
	if root != cgroupMountpoint {
		return fmt.Errorf("private cgroup namespace root resolved to %q, want %q", root, cgroupMountpoint)
	}
	if err := validateCgroupBounds(root); err != nil {
		return fmt.Errorf("validate private container cgroup: %w", err)
	}
	if err := limitNodeProcesses(root); err != nil {
		return fmt.Errorf("limit node processes: %w", err)
	}
	if err := prepareCgroupHierarchy(root, os.Getpid()); err != nil {
		return fmt.Errorf("delegate cgroup hierarchy: %w", err)
	}
	if err := unix.Mount("", "/", "", unix.MS_REC|unix.MS_SHARED, ""); err != nil {
		return fmt.Errorf("make node mount tree recursively shared: %w", err)
	}

	return syscall.Exec("/bin/k3s", []string{"/bin/k3s", "server", "--config=/etc/rancher/k3s/config.yaml"}, os.Environ())
}

func readCurrentCgroup(mountinfoFile, cgroupFile, expectedMountpoint string) (string, cgroupMount, string, error) {
	mountinfo, err := os.ReadFile(mountinfoFile)
	if err != nil {
		return "", cgroupMount{}, "", fmt.Errorf("read %s: %w", mountinfoFile, err)
	}
	currentCgroup, err := os.ReadFile(cgroupFile)
	if err != nil {
		return "", cgroupMount{}, "", fmt.Errorf("read %s: %w", cgroupFile, err)
	}
	mount, err := findCgroup2Mount(string(mountinfo), expectedMountpoint)
	if err != nil {
		return "", cgroupMount{}, "", err
	}
	membership, err := parseUnifiedCgroup(string(currentCgroup))
	if err != nil {
		return "", cgroupMount{}, "", err
	}
	cgroupDir, err := resolveCgroupPath(mount.root, mount.mountpoint, membership)
	if err != nil {
		return "", cgroupMount{}, "", err
	}
	return cgroupDir, mount, membership, nil
}

func currentCgroupDirectory(mountinfo, membershipFile, expectedMountpoint string) (string, error) {
	mount, err := findCgroup2Mount(mountinfo, expectedMountpoint)
	if err != nil {
		return "", err
	}
	membership, err := parseUnifiedCgroup(membershipFile)
	if err != nil {
		return "", err
	}
	return resolveCgroupPath(mount.root, mount.mountpoint, membership)
}

type cgroupMount struct {
	root       string
	mountpoint string
}

func findCgroup2Mount(mountinfo, expectedMountpoint string) (cgroupMount, error) {
	var found *cgroupMount
	for lineNumber, line := range strings.Split(strings.TrimSpace(mountinfo), "\n") {
		fields := strings.Fields(line)
		separator := -1
		for index, field := range fields {
			if field == "-" {
				separator = index
				break
			}
		}
		if separator < 0 || separator+3 >= len(fields) || separator < 6 {
			return cgroupMount{}, fmt.Errorf("mountinfo line %d has an invalid separator", lineNumber+1)
		}
		filesystem := fields[separator+1]
		if filesystem != "cgroup2" {
			continue
		}
		root, err := unescapeMountPath(fields[3])
		if err != nil {
			return cgroupMount{}, fmt.Errorf("mountinfo line %d root: %w", lineNumber+1, err)
		}
		mountpoint, err := unescapeMountPath(fields[4])
		if err != nil {
			return cgroupMount{}, fmt.Errorf("mountinfo line %d mountpoint: %w", lineNumber+1, err)
		}
		if !path.IsAbs(root) || !path.IsAbs(mountpoint) {
			return cgroupMount{}, fmt.Errorf("mountinfo line %d has a non-absolute cgroup path", lineNumber+1)
		}
		if path.Clean(mountpoint) == path.Clean(expectedMountpoint) {
			found = &cgroupMount{root: path.Clean(root), mountpoint: path.Clean(mountpoint)}
		}
	}
	if found == nil {
		return cgroupMount{}, fmt.Errorf("no cgroup2 mount at %q", expectedMountpoint)
	}
	return *found, nil
}

func unescapeMountPath(value string) (string, error) {
	var decoded strings.Builder
	decoded.Grow(len(value))
	for index := 0; index < len(value); index++ {
		if value[index] != '\\' {
			decoded.WriteByte(value[index])
			continue
		}
		if index+3 >= len(value) {
			return "", errors.New("truncated mountinfo escape")
		}
		escaped := value[index+1 : index+4]
		byteValue, err := strconv.ParseUint(escaped, 8, 8)
		if err != nil {
			return "", fmt.Errorf("invalid mountinfo escape %q: %w", escaped, err)
		}
		decoded.WriteByte(byte(byteValue))
		index += 3
	}
	return decoded.String(), nil
}

func parseUnifiedCgroup(contents string) (string, error) {
	var membership string
	for lineNumber, line := range strings.Split(strings.TrimSpace(contents), "\n") {
		fields := strings.SplitN(line, ":", 3)
		if len(fields) != 3 {
			return "", fmt.Errorf("cgroup line %d is malformed", lineNumber+1)
		}
		if fields[0] != "0" || fields[1] != "" {
			continue
		}
		if membership != "" {
			return "", errors.New("multiple unified cgroup memberships")
		}
		if !path.IsAbs(fields[2]) || path.Clean(fields[2]) != fields[2] {
			return "", fmt.Errorf("unified cgroup path %q is not clean and absolute", fields[2])
		}
		membership = fields[2]
	}
	if membership == "" {
		return "", errors.New("cgroup membership has no unified v2 entry")
	}
	return membership, nil
}

func resolveCgroupPath(mountRoot, mountpoint, membership string) (string, error) {
	if !path.IsAbs(mountRoot) || !path.IsAbs(mountpoint) || !path.IsAbs(membership) {
		return "", errors.New("cgroup mount and membership paths must be absolute")
	}
	mountRoot = path.Clean(mountRoot)
	mountpoint = path.Clean(mountpoint)
	membership = path.Clean(membership)
	if mountRoot == "/" {
		return path.Join(mountpoint, strings.TrimPrefix(membership, "/")), nil
	}
	if membership == mountRoot {
		return mountpoint, nil
	}
	if !strings.HasPrefix(membership, mountRoot+"/") {
		return "", fmt.Errorf("membership %q is outside cgroup mount root %q", membership, mountRoot)
	}
	return path.Join(mountpoint, strings.TrimPrefix(membership, mountRoot+"/")), nil
}

func validateInitialCgroup(mount cgroupMount, membership string) error {
	// A privileged CRI container must retain the host-relative cgroup path so `/` is distinguishable
	// from the container's actual scope before creating a narrower cgroup namespace.
	if mount.root != "/" {
		return fmt.Errorf("initial cgroup2 mount root is %q, want host hierarchy root /", mount.root)
	}
	if membership == "/" {
		return errors.New("initial cgroup membership is namespace-relative root /; cannot prove this is a container cgroup")
	}
	return nil
}

func validateCgroupBounds(cgroupDir string) error {
	if err := validateResourceLimits(cgroupDir); err != nil {
		return err
	}
	return requireControllersFile(cgroupDir, delegatedControllers)
}

func limitNodeProcesses(root string) error {
	filename := path.Join(root, "pids.max")
	contents, err := os.ReadFile(filename)
	if err != nil {
		return err
	}
	value := strings.TrimSpace(string(contents))
	if value != "max" {
		limit, err := strconv.ParseUint(value, 10, 64)
		if err != nil || limit == 0 {
			return fmt.Errorf("pids.max %q is not max or a positive count", value)
		}
		// Preserve a stricter limit supplied by the outer kubelet.
		if limit <= maxNodePIDs {
			return nil
		}
	}
	return writeCgroupValue(filename, strconv.FormatUint(maxNodePIDs, 10))
}

func validateResourceLimits(cgroupDir string) error {
	memory, err := os.ReadFile(path.Join(cgroupDir, "memory.max"))
	if err != nil {
		return fmt.Errorf("read finite memory limit: %w", err)
	}
	if err := parseFiniteMemoryLimit(string(memory)); err != nil {
		return err
	}
	cpu, err := os.ReadFile(path.Join(cgroupDir, "cpu.max"))
	if err != nil {
		return fmt.Errorf("read finite CPU limit: %w", err)
	}
	if err := parseFiniteCPUQuota(string(cpu)); err != nil {
		return err
	}
	return nil
}

func parseFiniteMemoryLimit(value string) error {
	value = strings.TrimSpace(value)
	if value == "" || value == "max" {
		return fmt.Errorf("memory.max %q is not a finite limit", value)
	}
	limit, err := strconv.ParseUint(value, 10, 64)
	if err != nil || limit == 0 {
		return fmt.Errorf("memory.max %q is not a positive finite byte limit", value)
	}
	return nil
}

func parseFiniteCPUQuota(value string) error {
	fields := strings.Fields(value)
	if len(fields) != 2 || fields[0] == "max" {
		return fmt.Errorf("cpu.max %q is not a finite quota and period", strings.TrimSpace(value))
	}
	quota, quotaErr := strconv.ParseUint(fields[0], 10, 64)
	period, periodErr := strconv.ParseUint(fields[1], 10, 64)
	if quotaErr != nil || periodErr != nil || quota == 0 || period == 0 {
		return fmt.Errorf("cpu.max %q is not a positive finite quota and period", strings.TrimSpace(value))
	}
	return nil
}

func requireControllersFile(cgroupDir string, required []string) error {
	contents, err := os.ReadFile(path.Join(cgroupDir, "cgroup.controllers"))
	if err != nil {
		return fmt.Errorf("read available cgroup controllers: %w", err)
	}
	available := parseControllerSet(string(contents))
	for _, controller := range required {
		if _, ok := available[controller]; !ok {
			return fmt.Errorf("cgroup controller %q is unavailable", controller)
		}
	}
	return nil
}

func parseControllerSet(value string) map[string]struct{} {
	controllers := make(map[string]struct{})
	for _, controller := range strings.Fields(value) {
		controllers[controller] = struct{}{}
	}
	return controllers
}

func requireCgroupNamespaceRoot(cgroupFile string) error {
	contents, err := os.ReadFile(cgroupFile)
	if err != nil {
		return fmt.Errorf("read cgroup namespace membership: %w", err)
	}
	membership, err := parseUnifiedCgroup(string(contents))
	if err != nil {
		return fmt.Errorf("verify cgroup namespace membership: %w", err)
	}
	if membership != "/" {
		return fmt.Errorf("new cgroup namespace membership is %q, want its root /", membership)
	}
	return nil
}

func prepareCgroupHierarchy(root string, pid int) error {
	if err := validateCgroupBounds(root); err != nil {
		return err
	}
	if err := requireNoChildCgroups(root); err != nil {
		return err
	}
	rootProcesses, err := readCgroupProcesses(path.Join(root, "cgroup.procs"))
	if err != nil {
		return err
	}
	if len(rootProcesses) != 1 || rootProcesses[0] != pid {
		return fmt.Errorf("container cgroup has processes %v, want only entrypoint PID %d", rootProcesses, pid)
	}

	if err := normalizeSubtreeControllers(root); err != nil {
		return err
	}
	if err := requireCgroupType(root, "domain"); err != nil {
		return err
	}

	controlDir := path.Join(root, controlCgroup)
	workloadsDir := path.Join(root, workloadsCgroup)
	if err := os.Mkdir(controlDir, 0o755); err != nil {
		return fmt.Errorf("create control cgroup: %w", err)
	}
	if err := os.Mkdir(workloadsDir, 0o755); err != nil {
		return fmt.Errorf("create workloads cgroup: %w", err)
	}
	if err := writeCgroupValue(path.Join(controlDir, "cgroup.procs"), strconv.Itoa(pid)); err != nil {
		return fmt.Errorf("move entrypoint into control cgroup: %w", err)
	}
	rootProcesses, err = readCgroupProcesses(path.Join(root, "cgroup.procs"))
	if err != nil {
		return err
	}
	if len(rootProcesses) != 0 {
		return fmt.Errorf("container cgroup still contains processes after moving entrypoint: %v", rootProcesses)
	}
	if err := requireCgroupType(root, "domain"); err != nil {
		return err
	}

	if err := enableControllers(root, delegatedControllers); err != nil {
		return err
	}
	if err := enableControllers(workloadsDir, delegatedControllers); err != nil {
		return err
	}
	if err := validateResourceLimits(root); err != nil {
		return fmt.Errorf("parent bounds changed during cgroup setup: %w", err)
	}
	return nil
}

func requireNoChildCgroups(cgroupDir string) error {
	entries, err := os.ReadDir(cgroupDir)
	if err != nil {
		return fmt.Errorf("read container cgroup children: %w", err)
	}
	for _, entry := range entries {
		if entry.IsDir() {
			return fmt.Errorf("container cgroup has pre-existing child cgroup %q", entry.Name())
		}
	}
	return nil
}

func readCgroupProcesses(file string) ([]int, error) {
	contents, err := os.ReadFile(file)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", file, err)
	}
	var processes []int
	for _, field := range strings.Fields(string(contents)) {
		pid, err := strconv.Atoi(field)
		if err != nil || pid <= 0 {
			return nil, fmt.Errorf("cgroup.procs contains invalid PID %q", field)
		}
		processes = append(processes, pid)
	}
	return processes, nil
}

func normalizeSubtreeControllers(root string) error {
	file := path.Join(root, "cgroup.subtree_control")
	contents, err := os.ReadFile(file)
	if err != nil {
		return fmt.Errorf("read existing subtree controllers: %w", err)
	}
	enabled := parseControllerSet(string(contents))
	for controller := range enabled {
		if controller != "pids" {
			return fmt.Errorf("container cgroup has unexpected enabled subtree controller %q", controller)
		}
	}
	if _, ok := enabled["pids"]; ok {
		if err := writeCgroupValue(file, "-pids"); err != nil {
			return fmt.Errorf("normalize existing pids delegation: %w", err)
		}
		contents, err := os.ReadFile(file)
		if err != nil {
			return fmt.Errorf("verify normalized subtree controllers: %w", err)
		}
		if len(parseControllerSet(string(contents))) != 0 {
			return fmt.Errorf("container cgroup subtree controllers remain enabled after normalization: %q", strings.TrimSpace(string(contents)))
		}
	}
	return nil
}

func requireCgroupType(cgroupDir, expected string) error {
	contents, err := os.ReadFile(path.Join(cgroupDir, "cgroup.type"))
	if err != nil {
		return fmt.Errorf("read cgroup type: %w", err)
	}
	actual := strings.TrimSpace(string(contents))
	if actual != expected {
		return fmt.Errorf("cgroup type is %q, want %q", actual, expected)
	}
	return nil
}

func enableControllers(cgroupDir string, controllers []string) error {
	if err := requireControllersFile(cgroupDir, controllers); err != nil {
		return err
	}
	var commands []string
	for _, controller := range controllers {
		commands = append(commands, "+"+controller)
	}
	file := path.Join(cgroupDir, "cgroup.subtree_control")
	if err := writeCgroupValue(file, strings.Join(commands, " ")); err != nil {
		return fmt.Errorf("enable cgroup controllers at %q: %w", cgroupDir, err)
	}
	contents, err := os.ReadFile(file)
	if err != nil {
		return fmt.Errorf("verify cgroup controllers at %q: %w", cgroupDir, err)
	}
	enabled := parseControllerSet(string(contents))
	for _, controller := range controllers {
		if _, ok := enabled[controller]; !ok {
			return fmt.Errorf("controller %q was not enabled at %q", controller, cgroupDir)
		}
	}
	return nil
}

func writeCgroupValue(file, value string) error {
	if err := os.WriteFile(file, []byte(value), 0); err != nil {
		return err
	}
	return nil
}
