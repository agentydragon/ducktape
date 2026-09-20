# OptiPlex Talos upgrade plan drift (2026-09-19)

The OptiPlex is still running Talos v1.12.3. The v1.14.0 Factory image using
the new `consoleblank=0` schematic resolves, but the targeted Terraform plan
for `talos_machine_configuration_apply.home_worker["optiplex"]` includes
machine-configuration drift beyond `machine.install.image`.

The extra change is
`ExtensionServiceConfig/nebula.configFiles[3].content`, mounted at
`/usr/local/etc/nebula/config.yml`. Comparing only field paths shows changes
to six `lighthouse.hosts` entries, six `relay.relays` entries, and
`static_host_map["10.42.0.19"][0]`. The current cluster maps `10.42.0.19` to
`ovh-ns1001419`. No Nebula values or secret material are recorded here.

Do not apply the combined machine configuration with the Talos OS upgrade.
For this approved version-only roll, use the exact v1.14.0 installer image with
`talosctl upgrade` and leave the Nebula roster refresh for separate review.
The Terraform plan is not proof that the node's machine configuration has
been reconciled.
