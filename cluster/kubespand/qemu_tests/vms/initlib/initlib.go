// Package initlib provides shared infrastructure for QEMU test VM init processes.
package initlib

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"

	qemu "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

var Role = "unknown"

func EmitEvent(evt qemu.Event) {
	b, _ := json.Marshal(evt)
	fmt.Println(string(b))
}

func Run(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func RunSilent(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Stdout = nil
	cmd.Stderr = nil
	return cmd.Run()
}

func MustRun(name string, args ...string) {
	if err := Run(name, args...); err != nil {
		EmitEvent(qemu.Event{Type: qemu.EventError, Message: fmt.Sprintf("%s failed: %v", name, err), Error: err.Error()})
		Poweroff()
	}
}

func Poweroff() {
	f, err := os.OpenFile("/proc/sysrq-trigger", os.O_WRONLY, 0)
	if err == nil {
		f.WriteString("o")
		f.Close()
	}
	time.Sleep(5 * time.Second)
	os.Exit(1)
}

func ParseCmdline() map[string]string {
	data, _ := os.ReadFile("/proc/cmdline")
	params := make(map[string]string)
	for _, arg := range strings.Fields(string(data)) {
		if i := strings.IndexByte(arg, '='); i >= 0 {
			params[arg[:i]] = arg[i+1:]
		}
	}
	return params
}

func LoadNftablesModules() {
	kvers, _ := os.ReadDir("/lib/modules")
	if len(kvers) == 0 {
		EmitEvent(qemu.Event{Type: qemu.EventError, Message: "no kernel modules found", Error: "empty /lib/modules/"})
		Poweroff()
	}
	kver := kvers[0].Name()
	RunSilent("modprobe", "crc32c_generic")
	if err := RunSilent("modprobe", "nf_tables"); err != nil {
		EmitEvent(qemu.Event{Type: qemu.EventError, Message: "modprobe nf_tables failed", Error: err.Error()})
	}
	EmitEvent(qemu.Event{Type: qemu.EventModules, Message: fmt.Sprintf("nftables modules loaded, kver=%s", kver)})
}

// HasInterface checks if a network interface appears within the given timeout.
// Returns true if found, false on timeout (does not power off).
func HasInterface(name string, timeout time.Duration) bool {
	path := "/sys/class/net/" + name
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return true
		}
		time.Sleep(200 * time.Millisecond)
	}
	return false
}

func WaitForInterface(name string) {
	path := "/sys/class/net/" + name
	for i := 0; i < 50; i++ {
		if _, err := os.Stat(path); err == nil {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	EmitEvent(qemu.Event{Type: qemu.EventError, Message: fmt.Sprintf("%s not found after 10s", name), Error: fmt.Sprintf("%s interface missing", name)})
	Poweroff()
}

func DumpLog(path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	os.Stderr.Write(data)
}

// ConfigureMgmtNIC brings up the QEMU user-mode management NIC (eth1) with
// the standard 10.0.2.15/24 address. If required is true, the function waits
// indefinitely for the interface; if false, it returns silently if eth1 doesn't
// appear within 2 seconds.
func ConfigureMgmtNIC(required bool) {
	if required {
		WaitForInterface("eth1")
	} else if !HasInterface("eth1", 2*time.Second) {
		return
	}
	MustRun("ip", "link", "set", "eth1", "up")
	MustRun("ip", "addr", "add", "10.0.2.15/24", "dev", "eth1")
}

// InitBasic performs common init setup: mount filesystems, set PATH, suppress dmesg.
func InitBasic() {
	os.Setenv("PATH", "/sbin:/usr/sbin:/bin:/usr/bin")
	syscall.Mount("proc", "/proc", "proc", 0, "")
	syscall.Mount("sys", "/sys", "sysfs", 0, "")
	syscall.Mount("dev", "/dev", "devtmpfs", 0, "")
	os.MkdirAll("/tmp", 0o755)
	os.MkdirAll("/run", 0o755)
	RunSilent("dmesg", "-n", "1")
}
