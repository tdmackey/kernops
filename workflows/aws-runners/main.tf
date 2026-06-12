# Ephemeral GitHub Actions runners on Graviton4 spot — Lambda-launched.
#
# Decision (June 2026): cheapest+fastest, biased toward speed when they
# conflict. So: big c8g spot instances, ephemeral (one job per instance),
# launched on webhook by the philips-labs scale-up Lambda, reaped by the
# scale-down Lambda. Per-build cost lands well under $1; wall clock ~8-12 min
# per flavour once the AMI is prebaked and ccache is S3-warmed.
#
# NOT APPLIED YET — fill the TODO variables (GitHub App, VPC) first.
# Docs: docs/ci-builders.md

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 5.0" }
  }
}

provider "aws" {
  region = var.aws_region
}

module "runners" {
  source  = "github-aws-runners/github-runner/aws"
  version = "~> 6.0"

  aws_region = var.aws_region
  vpc_id     = var.vpc_id
  subnet_ids = var.subnet_ids
  prefix     = "gb200-kernel"

  github_app = {
    key_base64     = var.github_app_key_base64
    id             = var.github_app_id
    webhook_secret = var.github_webhook_secret
  }

  # Speed-first sizing: Graviton5 (m9g) head, Graviton4 (c8g) fallback —
  # new-generation spot pools are shallow, so keep the fallbacks. Kernel
  # compile saturates ~64-96 cores; beyond that the serial deb stages
  # dominate. Swap in c9g when it reaches GA (2026, not yet released).
  instance_types               = ["m9g.24xlarge", "c8g.24xlarge", "c8g.16xlarge"]
  instance_target_capacity_type = "spot"
  instance_allocation_strategy  = "price-capacity-optimized"

  # One job per instance, then terminate. GitHub retries jobs that lose
  # their runner to a spot reclaim.
  enable_ephemeral_runners = true
  runner_os                = "linux"
  runner_architecture      = "arm64"
  runner_extra_labels      = ["kernel-builder", "arm64"]

  # Guardrails: a hung build cannot hold a 96-vCPU box hostage.
  runner_boot_time_in_minutes = 5
  minimum_running_time_in_minutes = 10
  runners_maximum_count       = 4
  scale_down_schedule_expression = "cron(*/5 * * * ? *)"

  # Prebaked builder AMI (Packer recipe TBD: build-deps + ccache + sbuild).
  # Until it exists, the default Ubuntu AMI works but wastes ~10 min/job on
  # apt. TODO: ami_filter pointing at the baked image.
  # ami_filter = { name = ["gb200-kernel-builder-*"], state = ["available"] }
}
