variable "aws_region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region for Route 53 API calls"
}

variable "route53_zone_id" {
  type        = string
  description = "Route 53 hosted zone ID for allegedly.works"
}

variable "public_nodes" {
  type        = map(object({ public_ip = string, role = string }))
  description = "Every Kubernetes node with a public endpoint, keyed by host name; role is the nebula-mesh.json role (control-plane or worker)"
}
