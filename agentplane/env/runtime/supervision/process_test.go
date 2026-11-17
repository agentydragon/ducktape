package supervision

import (
	"testing"
	"time"
)

func TestNewProcessRejectsNegativeStopGrace(t *testing.T) {
	_, err := (&Supervisor{}).NewProcess(ProcessConfig{
		Command:   []string{"/bin/true"},
		StopGrace: -time.Nanosecond,
	})
	if err == nil {
		t.Fatal("NewProcess accepted a negative stop grace")
	}
}

func TestNewProcessRejectsNegativeFenceTimeout(t *testing.T) {
	_, err := (&Supervisor{}).NewProcess(ProcessConfig{
		Command: []string{"/bin/true"}, FenceTimeout: -time.Nanosecond,
	})
	if err == nil {
		t.Fatal("NewProcess accepted a negative fence timeout")
	}
}
