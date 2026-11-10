"""The command box template the k3s fixture's sandbox Actions offer only here: the rendered command
box on the runner's imported images, with the runner disk's `agentplane` CLI, so a scenario can
create one through the Actions and drive the box's own egress. Production's command box image is not
imported into k3s."""

TEMPLATE = "systest-svid-box"
