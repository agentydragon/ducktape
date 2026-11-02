package supervision

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSystemdOwnerValidate(t *testing.T) {
	for _, test := range []struct {
		name  string
		owner SystemdOwner
	}{
		{name: "missing unit", owner: SystemdOwner{LifetimeDirectory: "/run/owner", ScopeID: "owner"}},
		{name: "relative lifetime directory", owner: SystemdOwner{Unit: "owner.service", LifetimeDirectory: "run/owner", ScopeID: "owner"}},
		{name: "missing scope ID", owner: SystemdOwner{Unit: "owner.service", LifetimeDirectory: "/run/owner"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := test.owner.validate(); err == nil {
				t.Fatal("invalid systemd owner was accepted")
			}
		})
	}
}

func TestSystemdScopeIdentity(t *testing.T) {
	const stateDir, sandboxUID = "/state/sandbox", "sandbox-uid"
	identities := make(map[string]SystemdScopeID)
	for _, scope := range []SystemdScopeID{SystemdEnvironmentRuntimeScopeID, "harness", "execution-service"} {
		owner := SystemdOwner{Unit: runtimeUnit, LifetimeDirectory: runtimeLifetimeDirectory, ScopeID: scope}
		identity := systemdScopeIdentity(stateDir, sandboxUID, owner)
		if want := stateDir + "\x00" + sandboxUID + "\x00" + string(scope); identity != want {
			t.Fatalf("scope %q identity = %q, want %q", scope, identity, want)
		}
		if previous, exists := identities[identity]; exists {
			t.Fatalf("scopes %q and %q shared a native process scope", previous, scope)
		}
		identities[identity] = scope
		if identity == systemdScopeIdentity(stateDir+"-other", sandboxUID, owner) || identity == systemdScopeIdentity(stateDir, sandboxUID+"-other", owner) {
			t.Fatalf("scope %q did not distinguish Sandbox state owners", scope)
		}
	}
}

func TestPrepareSystemdOwner(t *testing.T) {
	const invocationID = "0123456789abcdef0123456789abcdef"
	t.Setenv("INVOCATION_ID", invocationID)
	directory := t.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		t.Fatal(err)
	}
	owner := SystemdOwner{Unit: runtimeUnit, LifetimeDirectory: directory, ScopeID: "execution-service"}
	launcher := &SystemdLauncher{}
	if err := launcher.prepareOwner(owner); err != nil {
		t.Fatal(err)
	}
	if launcher.parentUnit() != owner.Unit {
		t.Fatalf("parent unit = %q, want %q", launcher.parentUnit(), owner.Unit)
	}
	wantMarker := filepath.Join(directory, invocationID)
	if launcher.lifetimeMarker != wantMarker {
		t.Fatalf("lifetime marker = %q, want %q", launcher.lifetimeMarker, wantMarker)
	}
	info, err := os.Stat(wantMarker)
	if err != nil {
		t.Fatal(err)
	}
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0444 {
		t.Fatalf("lifetime marker mode = %v, want read-only regular file", info.Mode())
	}
}
