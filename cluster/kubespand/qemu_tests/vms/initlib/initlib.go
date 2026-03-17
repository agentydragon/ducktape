// Package initlib provides shared infrastructure for QEMU test VM init processes.
package initlib

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vmconst"
)

var Role = "unknown"

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
		log.Fatalf("%s %v failed: %v", name, args, err)
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
		log.Fatalf("no kernel modules found: empty /lib/modules/")
	}
	kver := kvers[0].Name()
	RunSilent("modprobe", "crc32c_generic")
	if err := RunSilent("modprobe", "nf_tables"); err != nil {
		log.Printf("modprobe nf_tables failed: %v", err)
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
	log.Fatalf("%s not found after 10s", name)
}

func DumpLog(path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	os.Stderr.Write(data)
}

// Re-export vmconst values for convenience — initlib consumers don't need
// to import vmconst separately.
const (
	MgmtMAC = vmconst.MgmtMAC
	MgmtIP  = vmconst.MgmtIP
)

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
			log.Fatalf("mgmt NIC not found (MAC %s)", MgmtMAC)
		}
		return
	}
	MustRun("ip", "link", "set", iface, "up")
	MustRun("ip", "addr", "add", MgmtIP+"/24", "dev", iface)
}

// MountKubespandCIDATA mounts the CIDATA virtio drive and copies agent.yaml
// to /etc/kubespan/agent.yaml. The CIDATA drive is the first virtio block
// device (/dev/vda) for initramfs-only VMs (no root disk).
func MountKubespandCIDATA() {
	os.MkdirAll("/mnt/cidata", 0o755)
	os.MkdirAll("/etc/kubespan", 0o755)
	os.MkdirAll("/var/lib/kubespan", 0o755)

	MustRun("mount", "-t", "vfat", "-o", "ro", "/dev/vda", "/mnt/cidata")

	data, err := os.ReadFile("/mnt/cidata/agent.yaml")
	if err != nil {
		log.Fatalf("read CIDATA agent.yaml: %v", err)
	}
	if err := os.WriteFile("/etc/kubespan/agent.yaml", data, 0o644); err != nil {
		log.Fatalf("write /etc/kubespan/agent.yaml: %v", err)
	}
	log.Printf("kubespand config loaded from CIDATA (%d bytes)", len(data))
}

// Init performs common init setup (mount filesystems, set PATH, suppress dmesg)
// and parses kernel cmdline params. Sets Role from the "role" param if present.
func Init() map[string]string {
	InitBasic()
	params := ParseCmdline()
	if v, ok := params["role"]; ok {
		Role = v
	}
	return params
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
	// Redirect log output to stderr so it shows up in VM raw log (not parsed as events).
	log.SetOutput(os.Stderr)
	log.SetPrefix(fmt.Sprintf("[%s] ", Role))
}
