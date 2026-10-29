package centralsampler

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestHostCloseKillsProcessGroupDescendants(t *testing.T) {
	directory := t.TempDir()
	marker := filepath.Join(directory, "child.pid")
	binary := filepath.Join(directory, "host")
	script := fmt.Sprintf("#!/bin/sh\nsleep 60 &\nprintf '%%s\\n' \"$!\" > %q\nexit 0\n", marker)
	if err := os.WriteFile(binary, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}

	host, err := StartHost(context.Background(), binary, filepath.Join(directory, "host.log"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = host.Close() })
	childPIDText := waitForFile(t, marker)
	childPID, err := strconv.Atoi(strings.TrimSpace(childPIDText))
	if err != nil {
		t.Fatalf("parse child PID %q: %v", childPIDText, err)
	}
	if err := host.Wait(); err != nil {
		t.Fatalf("host leader exited with error: %v", err)
	}
	if !processGroupMemberRunning(childPID) {
		t.Fatal("test descendant exited before host cleanup")
	}

	if err := host.Close(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for processGroupMemberRunning(childPID) && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if processGroupMemberRunning(childPID) {
		t.Fatalf("descendant process %d survived host process-group kill", childPID)
	}
}

func waitForFile(t *testing.T, path string) string {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(path)
		if err == nil {
			return string(data)
		}
		if !errors.Is(err, os.ErrNotExist) {
			t.Fatal(err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("host did not create %s", path)
	return ""
}

func processGroupMemberRunning(pid int) bool {
	data, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "stat"))
	if errors.Is(err, os.ErrNotExist) {
		return false
	}
	if err != nil {
		return true
	}
	endCommand := strings.LastIndexByte(string(data), ')')
	if endCommand < 0 {
		return true
	}
	fields := strings.Fields(string(data)[endCommand+1:])
	return len(fields) == 0 || (fields[0] != "Z" && fields[0] != "X")
}
