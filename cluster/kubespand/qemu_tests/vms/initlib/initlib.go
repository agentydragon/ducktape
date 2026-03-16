// Package initlib provides shared infrastructure for QEMU test VM init processes.
package initlib

import (
	"encoding/json"
	"fmt"
	"log"
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
	log.Printf("nftables modules loaded, kver=%s", kver)
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

// MgmtMAC is the well-known MAC address assigned to the management NIC.
// BootVM on the test host always uses this MAC, allowing the VM init to
// find the mgmt NIC regardless of how many mesh NICs precede it.
const MgmtMAC = "52:54:00:aa:00:01"

// findMgmtNIC scans /sys/class/net for an interface with MgmtMAC.
func findMgmtNIC(timeout time.Duration) string {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		entries, _ := os.ReadDir("/sys/class/net")
		for _, e := range entries {
			if !strings.HasPrefix(e.Name(), "eth") {
				continue
			}
			mac, err := os.ReadFile("/sys/class/net/" + e.Name() + "/address")
			if err != nil {
				continue
			}
			if strings.TrimSpace(string(mac)) == MgmtMAC {
				return e.Name()
			}
		}
		time.Sleep(200 * time.Millisecond)
	}
	return ""
}

// ConfigureMgmtNIC brings up the QEMU user-mode management NIC with the
// standard 10.0.2.15/24 address. The NIC is identified by its well-known
// MAC address (MgmtMAC), so it works regardless of how many mesh NICs
// precede it. If required is true, the function waits up to 10s; if false,
// it returns silently if the NIC doesn't appear within 2s.
func ConfigureMgmtNIC(required bool) {
	var iface string
	if required {
		iface = findMgmtNIC(10 * time.Second)
	} else {
		iface = findMgmtNIC(2 * time.Second)
	}
	if iface == "" {
		if required {
			EmitEvent(qemu.Event{Type: qemu.EventError, Message: "mgmt NIC not found (MAC " + MgmtMAC + ")"})
			Poweroff()
		}
		return
	}
	MustRun("ip", "link", "set", iface, "up")
	MustRun("ip", "addr", "add", "10.0.2.15/24", "dev", iface)
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
