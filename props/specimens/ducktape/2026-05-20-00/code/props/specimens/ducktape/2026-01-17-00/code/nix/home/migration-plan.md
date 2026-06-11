# Nix/Home-Manager Migration Plan

## Strategy: Parallel Deployment with Progressive Cutover

Your proposed strategy makes perfect sense! Here's the refined plan:

### Phase 1: Test & Mark

**Status: Testing Complete, Marking Pending**

#### K8s Sandbox Testing Results (2025-08-28)

- ✅ Successfully built home.nix with nixpkgs 24.05 (now updated to 25.05)
- ✅ All packages installed correctly with Python 3.12 (changed from 3.13 for numpy compatibility)
- ✅ Generated .claude.json with correct MCP server configuration
- ✅ File linking works (manual activation required in container due to dbus)
- ⚠️ Note: Used single-user Nix installation in container (no daemon needed)
- ⚠️ Note: home-manager must be installed via `programs.home-manager.enable = true`, not nix-env

#### What We Had to Skip/Disable

- ❌ **XDG MIME associations** (mimeapps.list): Home-manager would replace all 105 associations with just 2. Kept in Ansible to preserve existing associations.
- ❌ **Claude MCP configuration** (.claude.json): File contains many other Claude settings beyond MCP servers. Need in-place editing solution, not file replacement.
- ❌ **NPM global packages** (jscpd, madge, @openai/codex@0.53.0): Not available in nixpkgs. Users must install manually with: `pnpm add -g jscpd madge @openai/codex@0.53.0`

- [ ] Automate bumping of @openai/codex via CI (e.g. GitHub Actions schedule and PR workflow)
- ⚠️ **Package conflicts**: Some packages installed via both Nix and system (ruff, gh). This is harmless but creates duplication.

#### Ansible Role Split (2025-08-28)

**STATUS: COMPLETED**

Split roles into migrated and unmigrated parts:

- ✅ Created `gui_nix_migrated` with dconf/autostart tasks
- ✅ Created `gnome-terminal-solarized_nix_migrated` with profile creation
- ✅ Created `cli_nix_migrated` (placeholder for migrated packages)
- ✅ Renamed `dev-ml` → `dev-ml_nix_migrated` (fully migrated)
- ✅ Updated wyrm.yaml, agentydragon.yaml, atlas.yaml to exclude `*_nix_migrated` roles (all Nix-managed)
- ⏳ gpd.yaml and vps.yaml still include both versions (not yet migrated to Nix)

### Phase 2: Wyrm Deployment

**Current Status: Completed - Roles split and playbooks updated**

Deployment approach with split roles:

1. **Install Nix** (if not already installed):

```bash
curl -L https://nixos.org/nix/install | sh -s -- --daemon
```

2. **Install home-manager**:

```bash
nix-channel --add https://github.com/nix-community/home-manager/archive/release-25.05.tar.gz home-manager
nix-channel --add https://nixos.org/channels/nixos-25.05 nixpkgs  # Ensure 25.05 for compatibility
nix-channel --update
nix-shell '<home-manager>' -A install
```

3. **Deploy home.nix**:

```bash
cd ~/code/ducktape/nix/home
home-manager switch -f home.nix
```

4. **Run Ansible for wyrm** (automatically skips migrated roles):

```bash
cd ~/code/ducktape/ansible
ansible-playbook ansible/wyrm.yaml --ask-become-pass
```

This will:

- Skip `dev-ml_nix_migrated` (not included in wyrm.yaml)
- Run only unmigrated parts of `gui` and `cli` roles
- Avoid duplicate configuration since migrated tasks were moved to separate roles

### Phase 3: Testing Checklist

After deployment, verify:

- [ ] GNOME Terminal has both Solarized profiles
- [ ] Night Theme Switcher triggers theme switching
- [ ] Flameshot launches with Print key
- [ ] Autostart applications work (Syncthing-GTK, Discord, Flameshot)
- [ ] Claude Code MCP servers are configured
- [ ] Workspace switching shortcuts work (Ctrl+Alt+↑/↓)

### Phase 4: Progressive Migration

#### Still to Migrate (wyrm-specific)

1. **cli role** → home.nix:
   - Dotfiles management (rcup)
   - Shell configuration
   - Build dependencies

2. **dev-env role** → home.nix:
   - Development environment setup

3. **dev-ml role** → home.nix:
   - ML packages (already partially in home.nix)

4. **k3s-client role** → home.nix or configuration.nix:
   - Kubeconfig setup

5. **Wyrm-specific tasks**:
   - Pip cache on tankshare
   - Screen blanking (already in dconf)

### Phase 5: Full Cutover

Once tested and stable on all machines:

1. Delete `*_nix_migrated` roles from Ansible
2. Remove imports of migrated roles from non-wyrm playbooks
3. Update documentation
4. Consider migrating remaining parts of cli and gui roles

## TODO Items

### Kubernetes Sandbox Setup

- [ ] Transfer ansible vault key/secrets to k8s sandbox for testing
  - Save vault key: `secret-tool lookup service ansible-vault account ducktape`
  - Transfer to sandbox securely
  - Or temporarily disable vault-encrypted values for testing

## Implementation Notes

### Role Split Strategy

Instead of tagging individual tasks, roles were split:

- Base role (e.g., `gui`) - contains unmigrated tasks only
- Migrated role (e.g., `gui_nix_migrated`) - contains tasks migrated to Nix
- Wyrm playbook excludes `*_nix_migrated` roles
- Other playbooks include both versions for compatibility

### Key Learnings

#### From K8s Testing

1. **Python Version**: Use Python 3.12, not 3.13 (numpy compatibility)
2. **Nix Channels**: Must use matching versions (e.g., nixpkgs 25.05 with home-manager 25.05)
3. **Installation Order**:
   - Don't install home-manager via nix-env
   - Let `programs.home-manager.enable = true` handle it
4. **File Conflicts**: May need to remove existing files before first activation
5. **dbus Issues**: Expected in containers, won't affect real systems

#### From Atlas Deployment (Debian/Proxmox)

1. **Shell Initialization**: Debian's `/etc/profile` resets PATH unconditionally, breaking Nix paths
   - Solution: Don't source `/etc/profile` in `.shellrc`
2. **Installation Cleanup**: Failed root installations leave artifacts that block reinstalls
   - Check for: backup files, nixbld users/groups, `/nix` directory
3. **Multi-user Setup**: Requires `NIX_REMOTE=daemon` for proper operation

### Rollback Strategy

If issues arise:

```bash
# Rollback home-manager
home-manager generations  # List generations
home-manager rollback     # Go to previous

# Re-enable Ansible tasks (once tags are added)
ansible-playbook ansible/wyrm.yaml --ask-become-pass  # Without skip-tags
```

## Benefits of This Approach

1. **No service disruption** - both systems coexist
2. **Easy rollback** - can revert to pure Ansible quickly
3. **Gradual validation** - test each component separately
4. **Machine-by-machine migration** - proven on wyrm before others
5. **Clear tracking** - tagged tasks show migration progress
