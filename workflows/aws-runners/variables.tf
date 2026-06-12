variable "aws_region" {
  type    = string
  default = "us-west-2"
}

# TODO: VPC with public subnets (or private + NAT) in >=2 AZs for spot depth.
variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

# TODO: create a GitHub App on the org (philips-labs setup docs), store the
# key in SSM/secrets manager — never in this repo.
variable "github_app_id" {
  type = string
}

variable "github_app_key_base64" {
  type      = string
  sensitive = true
}

variable "github_webhook_secret" {
  type      = string
  sensitive = true
}
