package supervision

import (
	"sort"
	"syscall"

	"golang.org/x/sys/unix"
)

func environment(values map[string]string) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := make([]string, 0, len(keys))
	for _, key := range keys {
		result = append(result, key+"="+values[key])
	}
	return result
}

func signalProcessGroup(leader int, signal syscall.Signal) error {
	err := unix.Kill(-leader, unix.Signal(signal))
	if err == unix.ESRCH {
		return nil
	}
	return err
}
