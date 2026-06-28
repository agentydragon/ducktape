# Rename the OVH node for_each keys from the role-encoded names (which lie —
# kimsufi_worker0/1 are control planes) to the role-neutral provider hostnames,
# matching the already-renamed Talos/k8s node names. State-only: moved blocks keep
# every instance in place (tofu plan must show 0 destroy/recreate).

moved {
  from = null_resource.install_talos_kimsufi["kimsufi_worker0"]
  to   = null_resource.install_talos_kimsufi["ovh-ns103656"]
}

moved {
  from = null_resource.install_talos_kimsufi["kimsufi_worker1"]
  to   = null_resource.install_talos_kimsufi["ovh-ns103711"]
}

moved {
  from = null_resource.install_talos_kimsufi["ks_game_worker0"]
  to   = null_resource.install_talos_kimsufi["ovh-ns104952"]
}

moved {
  from = null_resource.install_talos_kimsufi["ks_game_worker1"]
  to   = null_resource.install_talos_kimsufi["ovh-ns104963"]
}

moved {
  from = ovh_dedicated_server.kimsufi["kimsufi_worker0"]
  to   = ovh_dedicated_server.kimsufi["ovh-ns103656"]
}

moved {
  from = ovh_dedicated_server.kimsufi["kimsufi_worker1"]
  to   = ovh_dedicated_server.kimsufi["ovh-ns103711"]
}

moved {
  from = ovh_dedicated_server.kimsufi["ks_game_worker0"]
  to   = ovh_dedicated_server.kimsufi["ovh-ns104952"]
}

moved {
  from = ovh_dedicated_server.kimsufi["ks_game_worker1"]
  to   = ovh_dedicated_server.kimsufi["ovh-ns104963"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_to_rescue["kimsufi_worker0"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_to_rescue["ovh-ns103656"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_to_rescue["kimsufi_worker1"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_to_rescue["ovh-ns103711"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_to_rescue["ks_game_worker0"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_to_rescue["ovh-ns104952"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_to_rescue["ks_game_worker1"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_to_rescue["ovh-ns104963"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_to_talos["kimsufi_worker0"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_to_talos["ovh-ns103656"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_to_talos["kimsufi_worker1"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_to_talos["ovh-ns103711"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_to_talos["ks_game_worker0"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_to_talos["ovh-ns104952"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_to_talos["ks_game_worker1"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_to_talos["ovh-ns104963"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_harddisk["kimsufi_worker0"]
  to   = ovh_dedicated_server_update.kimsufi_harddisk["ovh-ns103656"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_harddisk["kimsufi_worker1"]
  to   = ovh_dedicated_server_update.kimsufi_harddisk["ovh-ns103711"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_harddisk["ks_game_worker0"]
  to   = ovh_dedicated_server_update.kimsufi_harddisk["ovh-ns104952"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_harddisk["ks_game_worker1"]
  to   = ovh_dedicated_server_update.kimsufi_harddisk["ovh-ns104963"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_rescue["kimsufi_worker0"]
  to   = ovh_dedicated_server_update.kimsufi_rescue["ovh-ns103656"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_rescue["kimsufi_worker1"]
  to   = ovh_dedicated_server_update.kimsufi_rescue["ovh-ns103711"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_rescue["ks_game_worker0"]
  to   = ovh_dedicated_server_update.kimsufi_rescue["ovh-ns104952"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_rescue["ks_game_worker1"]
  to   = ovh_dedicated_server_update.kimsufi_rescue["ovh-ns104963"]
}

moved {
  from = talos_machine_configuration_apply.kimsufi["kimsufi_worker0"]
  to   = talos_machine_configuration_apply.kimsufi["ovh-ns103656"]
}

moved {
  from = talos_machine_configuration_apply.kimsufi["kimsufi_worker1"]
  to   = talos_machine_configuration_apply.kimsufi["ovh-ns103711"]
}

moved {
  from = talos_machine_configuration_apply.kimsufi["ks_game_worker0"]
  to   = talos_machine_configuration_apply.kimsufi["ovh-ns104952"]
}

moved {
  from = talos_machine_configuration_apply.kimsufi["ks_game_worker1"]
  to   = talos_machine_configuration_apply.kimsufi["ovh-ns104963"]
}

moved {
  from = null_resource.install_talos_kimsufi_cp["kimsufi_cp0"]
  to   = null_resource.install_talos_kimsufi_cp["ovh-ns102453"]
}

moved {
  from = ovh_dedicated_server.kimsufi_cp["kimsufi_cp0"]
  to   = ovh_dedicated_server.kimsufi_cp["ovh-ns102453"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_cp_to_rescue["kimsufi_cp0"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_cp_to_rescue["ovh-ns102453"]
}

moved {
  from = ovh_dedicated_server_reboot_task.kimsufi_cp_to_talos["kimsufi_cp0"]
  to   = ovh_dedicated_server_reboot_task.kimsufi_cp_to_talos["ovh-ns102453"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_cp_harddisk["kimsufi_cp0"]
  to   = ovh_dedicated_server_update.kimsufi_cp_harddisk["ovh-ns102453"]
}

moved {
  from = ovh_dedicated_server_update.kimsufi_cp_rescue["kimsufi_cp0"]
  to   = ovh_dedicated_server_update.kimsufi_cp_rescue["ovh-ns102453"]
}

moved {
  from = talos_machine_configuration_apply.kimsufi_cp["kimsufi_cp0"]
  to   = talos_machine_configuration_apply.kimsufi_cp["ovh-ns102453"]
}
