package runtime

import "testing"

func TestStoppedPackagedUnitGroup(t *testing.T) {
	unit := map[string]any{
		"Id": HostUnitName, "LoadState": "loaded", "ActiveState": "inactive",
		"SubState": "dead", "InvocationID": "previous",
	}
	service := map[string]any{"MainPID": uint32(0), "Slice": "agentplane.slice"}
	if group, err := stoppedPackagedUnitGroup(HostUnitName, unit, service); err != nil || group != "/agentplane.slice/"+HostUnitName {
		t.Fatalf("stopped host group = %q, %v", group, err)
	}
	execution := map[string]any{
		"Id": ExecutionUnitName, "LoadState": "loaded", "ActiveState": "failed",
		"SubState": "failed", "InvocationID": "previous",
	}
	if group, err := stoppedPackagedUnitGroup(ExecutionUnitName, execution, service); err != nil || group != "/agentplane.slice/"+ExecutionUnitName {
		t.Fatalf("stopped execution group = %q, %v", group, err)
	}
	for _, test := range []struct {
		name     string
		unitName string
		unit     map[string]any
		service  map[string]any
	}{
		{"wrong unit", HostUnitName, map[string]any{"Id": ExecutionUnitName, "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "InvocationID": "previous"}, service},
		{"unknown packaged unit", "other.service", map[string]any{"Id": "other.service", "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "InvocationID": "previous"}, service},
		{"active", HostUnitName, map[string]any{"Id": HostUnitName, "LoadState": "loaded", "ActiveState": "active", "SubState": "running", "InvocationID": "previous"}, service},
		{"no previous invocation", HostUnitName, map[string]any{"Id": HostUnitName, "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead"}, service},
		{"pid remains", HostUnitName, unit, map[string]any{"MainPID": uint32(7), "Slice": "agentplane.slice"}},
		{"different slice", HostUnitName, unit, map[string]any{"MainPID": uint32(0), "Slice": "other.slice"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			if group, err := stoppedPackagedUnitGroup(test.unitName, test.unit, test.service); err == nil || group != "" {
				t.Fatalf("unverifiable group = %q, %v", group, err)
			}
		})
	}
}

func TestSystemdUnitNeverStarted(t *testing.T) {
	unit := map[string]any{"LoadState": "loaded", "ActiveState": "inactive", "InvocationID": ""}
	service := map[string]any{"MainPID": uint32(0)}
	tests := []struct {
		name     string
		unit     map[string]any
		service  map[string]any
		expected unitInvocation
		want     bool
	}{
		{name: "loaded inactive unit with no invocation", unit: unit, service: service, want: true},
		{
			name:    "active unit",
			unit:    map[string]any{"LoadState": "loaded", "ActiveState": "active", "InvocationID": ""},
			service: service,
		},
		{
			name:    "unit with a previous invocation",
			unit:    map[string]any{"LoadState": "loaded", "ActiveState": "inactive", "InvocationID": "previous"},
			service: service,
		},
		{
			name:    "unit with a remaining process",
			unit:    unit,
			service: map[string]any{"MainPID": uint32(12)},
		},
		{
			name:    "unit with a recorded cgroup",
			unit:    unit,
			service: map[string]any{"MainPID": uint32(0), "ControlGroup": "/agentplane.slice/host.service"},
		},
		{
			name:    "expected invocation",
			unit:    unit,
			service: service,
			expected: unitInvocation{
				ID: "expected",
			},
		},
		{
			name:    "expected cgroup",
			unit:    unit,
			service: service,
			expected: unitInvocation{
				ControlGroup: "/agentplane.slice/host.service",
			},
		},
		{
			name:    "unloaded unit",
			unit:    map[string]any{"LoadState": "not-found", "ActiveState": "inactive", "InvocationID": ""},
			service: service,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := systemdUnitNeverStarted(test.unit, test.service, test.expected); got != test.want {
				t.Fatalf("systemdUnitNeverStarted() = %v, want %v", got, test.want)
			}
		})
	}
}

func TestSystemdInvocationSucceeded(t *testing.T) {
	tests := []struct {
		name    string
		service map[string]any
		want    bool
	}{
		{
			name: "systemd stopped the unit without recording a main process exit",
			service: map[string]any{
				"Result": "success", "ExecMainCode": int32(0), "ExecMainStatus": int32(0),
			},
			want: true,
		},
		{
			name: "main process exited successfully",
			service: map[string]any{
				"Result": "success", "ExecMainCode": int32(1), "ExecMainStatus": int32(0),
			},
			want: true,
		},
		{
			name: "systemd reports an exit failure",
			service: map[string]any{
				"Result": "exit-code", "ExecMainCode": int32(1), "ExecMainStatus": int32(1),
			},
		},
		{
			name:    "missing result",
			service: map[string]any{"ExecMainCode": int32(1), "ExecMainStatus": int32(0)},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := systemdInvocationSucceeded(test.service); got != test.want {
				t.Fatalf("systemdInvocationSucceeded() = %v, want %v", got, test.want)
			}
		})
	}
}
