output "ssh_key_fingerprints" {
  description = "Forgejo SSH key fingerprints attached to agentydragon."
  value = {
    for host, key in forgejo_ssh_key.host :
    host => key.fingerprint
  }
}
