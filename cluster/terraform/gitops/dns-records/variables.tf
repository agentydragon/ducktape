variable "aws_region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region for Route 53 API calls"
}

variable "route53_zone_id" {
  type        = string
  description = "Route 53 hosted zone ID for allegedly.works"
}
