# Linux Desktop VM Provisioning (Manual Steps)

These commands produce the `linux-desktop-01` virtual machine on `atlas` (Proxmox). Run them manually before applying the Ansible playbook.

1. **Clone from template**

   ```bash
   qm clone 9000 1301 --name linux-desktop-01 --full
   ```

2. **Allocate resources**

   ```bash
   qm set 1301 --cores 4 --memory 8192
   qm resize 1301 scsi0 40G
   ```

3. **Configure cloud-init (static IP example)**

   ```bash
   cat ~/.ssh/id_ed25519.pub > /root/.ssh/linux-desktop-ci.pub   # ensure your public key exists on atlas

   qm set 1301 \
     --ipconfig0 ip=10.0.201.51/16,gw=10.0.0.1 \
     --nameserver 8.8.8.8 \
     --ciuser agentydragon \
     --sshkeys /root/.ssh/linux-desktop-ci.pub

   qm cloudinit update 1301
   ```

4. **Start the VM and wait for cloud-init**

   ```bash
   qm start 1301
   sleep 60
   ```

5. **Inventory entry**
   Add the host to `ansible/inventory.yaml` (or use `--limit`):

   ```yaml
   linux_desktop:
     hosts:
       linux-desktop-01:
         ansible_host: 10.0.201.51
         ansible_user: agentydragon
         ansible_ssh_private_key_file: ~/.ssh/id_ed25519
   ```

6. **Install the RAC SSH public key**

   Helm now renders the Guacamole SSH key into a ConfigMap. Pull it once the cluster has applied the chart and drop it into `authorized_keys` for the login user:

   ```bash
   kubectl -n authentik get configmap linux-desktop-rac-ssh-public-key \
     -o jsonpath='{.data.public-key}' \
     > /tmp/linux-desktop-rac.pub

   ssh agentydragon@10.0.201.51 \
     'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys' \
     < /tmp/linux-desktop-rac.pub
   ```

   (You can instead automate this with Ansible’s `authorized_key` module if you prefer.)

After these steps, run the playbook to install the GUI packages and XRDP:

```bash
cd ~/code/ducktape/ansible
cd ansible
ansible-playbook linux-desktop.yaml
```
