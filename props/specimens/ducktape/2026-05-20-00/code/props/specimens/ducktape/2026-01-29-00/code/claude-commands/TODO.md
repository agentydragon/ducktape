# Claude Code Commands Deployment

## Purpose

These Claude Code commands should be deployed to `/code/.claude/commands/`.

The SSOT (Single Source of Truth) lives here in the ducktape repository, and the commands are deployed to `/code` as symlinks.

## Manual Deployment

To deploy the commands:

```bash
mkdir -p /code/.claude/commands
ln -sf ~/code/ducktape/claude-commands/organize-code.md /code/.claude/commands/organize-code.md
```

## TODO: Automate with Ansible

The deployment should be automated with an Ansible role or task. Options:

1. **Add to existing role** (e.g., `dev_env`) - if all dev machines should have these commands
2. **Create new role** (`claude_code`) - for Claude-specific setup
3. **Add as tasks in playbook** - if it's machine-specific

Example Ansible task structure:

```yaml
- name: Ensure /code/.claude/commands directory exists
  ansible.builtin.file:
    path: /code/.claude/commands
    state: directory
    mode: "0755"

- name: Symlink Claude commands from ducktape
  ansible.builtin.file:
    src: "{{ ansible_env.HOME }}/code/ducktape/claude-commands/{{ item }}"
    dest: "/code/.claude/commands/{{ item }}"
    state: link
    force: true
  loop:
    - organize-code.md
```

### Caveats

**Shared ZFS dataset across multiple machines**: The ZFS dataset `tank/code` is accessed from multiple machines with different mount points:

- **atlas** (Proxmox host): `/tank/code` (native ZFS mount)
- **wyrm** (Pop!_OS VM): `/code` (virtiofs mount of atlas's `/tank/code`)

**Current issue**: The existing symlink at `/code/.claude/commands/organize-code.md` incorrectly points to `/home/agentydragon/code/ducktape/claude-commands/organize-code.md`, which is machine-specific.

**Repository structure**:

- atlas: `/tank/code/gitlab.com/agentydragon/ducktape`
- wyrm: `/code/gitlab.com/agentydragon/ducktape` (same physical storage)
- Convenience symlink: `~/code/ducktape` → `/code/gitlab.com/agentydragon/ducktape` (machine-specific)

**Why absolute paths don't work**: Since the ZFS dataset is mounted at different paths on different machines, an absolute symlink like `/code/gitlab.com/...` would:

- Work on wyrm (where `/code` exists)
- Fail on atlas (where it's `/tank/code`, not `/code`)

**Solution**: Use **relative symlinks** that work regardless of mount point:

```bash
ln -sf ../../gitlab.com/agentydragon/ducktape/claude-commands/organize-code.md organize-code.md
```

Both create the same relative symlink in the shared filesystem, which resolves correctly from either mount point.

**For automation**: Since the ZFS dataset is shared, the symlink only needs to be created ONCE from either machine. Deploy from the host where Claude Code actually runs (likely wyrm):

```yaml
# Add to wyrm.yaml playbook only (not atlas.yaml)
- name: Determine shared code base path
  ansible.builtin.set_fact:
    shared_code_base: "{{ '/tank/code' if inventory_hostname == 'atlas' else '/code' }}"
  tags: [claude-code]

- name: Ensure .claude/commands directory exists on shared storage
  ansible.builtin.file:
    path: "{{ shared_code_base }}/.claude/commands"
    state: directory
    owner: "{{ my_user }}"
    group: "{{ my_user }}"
    mode: "0755"
  tags: [claude-code]

- name: Symlink Claude commands using relative paths
  ansible.builtin.command:
    cmd: ln -sf ../../gitlab.com/agentydragon/ducktape/claude-commands/{{ item }} {{ item }}
    chdir: "{{ shared_code_base }}/.claude/commands"
  args:
    creates: "{{ shared_code_base }}/.claude/commands/{{ item }}"
  loop:
    - organize-code.md
  tags: [claude-code]
```

**Important**: Only run this from ONE host (recommend: wyrm). Since the symlink lives in the shared ZFS dataset, it will automatically be visible from all machines that mount it.
