# Set by cluster/cdk8s/monitoring/home_switch.py (HomeSwitchVars).

variable "switch_address" {
  type        = string
  description = "The switch's management address, which its certificates name."
}

variable "agentplane_ssh_public_key" {
  type        = string
  description = "Public SSH key for the Agentplane switch user."
}

variable "lan_cidr" {
  type        = string
  description = "Home LAN range allowed to reach switch management services."
}

variable "lan_gateway" {
  type        = string
  description = "Home gateway address used as the Nebula container DNS server."
}

variable "nebula_image" {
  type        = string
  description = "Public ARMv7 Nebula container image to run on the CRS310."
}

variable "syslog_address" {
  type        = string
  description = "Where the switch sends its log as BSD syslog over UDP."
}

variable "syslog_port" {
  type        = number
  description = "The UDP port at syslog_address."
}
