package runner

import (
	"testing"
)

func TestResourceLimitsRequirePositiveValues(t *testing.T) {
	valid := ResourceLimits{
		CPUQuotaMicros: 100000, MemoryMaxBytes: 64 * 1024 * 1024, PidsMax: 32,
		ReadBPS: 1, WriteBPS: 1, ReadIOPS: 1, WriteIOPS: 1,
	}
	if err := valid.Validate(); err != nil {
		t.Fatalf("valid resource limits rejected: %v", err)
	}
	for name, update := range map[string]func(*ResourceLimits){
		"CPU":        func(limits *ResourceLimits) { limits.CPUQuotaMicros = 0 },
		"memory":     func(limits *ResourceLimits) { limits.MemoryMaxBytes = 0 },
		"PIDs":       func(limits *ResourceLimits) { limits.PidsMax = 0 },
		"read BPS":   func(limits *ResourceLimits) { limits.ReadBPS = 0 },
		"write BPS":  func(limits *ResourceLimits) { limits.WriteBPS = 0 },
		"read IOPS":  func(limits *ResourceLimits) { limits.ReadIOPS = 0 },
		"write IOPS": func(limits *ResourceLimits) { limits.WriteIOPS = 0 },
	} {
		t.Run(name, func(t *testing.T) {
			invalid := valid
			update(&invalid)
			if err := invalid.Validate(); err == nil {
				t.Fatal("invalid resource limits accepted")
			}
		})
	}
}

func TestProcessResourceLimitsMustFitAggregate(t *testing.T) {
	aggregate := ResourceLimits{
		CPUQuotaMicros: 100000, MemoryMaxBytes: 128 * 1024 * 1024, PidsMax: 64,
		ReadBPS: 1, WriteBPS: 1, ReadIOPS: 1, WriteIOPS: 1,
	}
	process := ResourceLimits{
		CPUQuotaMicros: 100000, MemoryMaxBytes: 64 * 1024 * 1024, PidsMax: 32,
		ReadBPS: 1, WriteBPS: 1, ReadIOPS: 1, WriteIOPS: 1,
	}
	groups := &VMProcessPolicy{AggregateLimits: aggregate}
	if err := groups.validateProcessLimits(process); err != nil {
		t.Fatalf("valid process limits rejected: %v", err)
	}
	process.MemoryMaxBytes = aggregate.MemoryMaxBytes + 1
	if err := groups.validateProcessLimits(process); err == nil {
		t.Fatal("process memory max larger than aggregate accepted")
	}
}
