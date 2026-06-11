# Intel Lunar Lake NPU driver setup for LLM inference
#
# Enables the NPU kernel driver and userspace stack. LLM inference runs via
# llama.cpp with OpenVINO backend in a Docker container (see debug/rugged/hw/llm_npu.md).
#
# TODO: Nixify the llama-openvino:server container as a podman service
# (like local_llm_arc does for Arc GPU).
#
# Hardware: Intel Lunar Lake NPU (PCI 8086:643e), /dev/accel/accel0.
{
  config,
  lib,
  username,
  ...
}:
let
  cfg = config.ducktape.localLlm.npu;
in
{
  options.ducktape.localLlm.npu = {
    enable = lib.mkEnableOption "Intel NPU driver setup for LLM inference";
  };

  config = lib.mkIf cfg.enable {
    hardware.cpu.intel.npu.enable = true;

    # Grant user access to /dev/accel/*
    users.users.${username}.extraGroups = [ "render" ];
  };
}
