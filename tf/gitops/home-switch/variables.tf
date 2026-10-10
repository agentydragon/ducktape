# Set by cluster/cdk8s/monitoring/home_switch.py (HomeSwitchVars).

variable "switch_address" {
  type        = string
  description = "The switch's management address, which its certificates name."
}

variable "lan_cidr" {
  type        = string
  description = "The only network management services and the monitoring user accept connections from."
}

variable "syslog_address" {
  type        = string
  description = "Where the switch sends its log as BSD syslog over UDP."
}

variable "syslog_port" {
  type        = number
  description = "The UDP port at syslog_address."
}
