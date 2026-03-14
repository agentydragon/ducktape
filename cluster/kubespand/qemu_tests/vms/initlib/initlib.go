// Package initlib provides shared infrastructure for QEMU test VM init processes.
package initlib

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"

	qemu "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

var Role = "unknown"

func EmitEvent(evt qemu.Event) {
	evt.Timestamp = float64(Uptime())
	evt.Role = Role
	b, _ := json.Marshal(evt)
	fmt.Println(string(b))
}

func Uptime() int64 {
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	parts := strings.Fields(string(data))
	if len(parts) == 0 {
		return 0
	}
	dotIdx := strings.Index(parts[0], ".")
	if dotIdx < 0 {
		v, _ := strconv.ParseInt(parts[0], 10, 64)
		return v
	}
	v, _ := strconv.ParseInt(parts[0][:dotIdx], 10, 64)
	return v
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
