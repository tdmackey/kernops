# CI builders — ephemeral Graviton4 spot runners

Decision (June 2026): **cheapest + fastest, biased toward speed on conflict.**

## Architecture

[philips-labs/terraform-aws-github-runner](https://github.com/github-aws-runners/terraform-aws-github-runner)
— GitHub webhook (`workflow_job: queued`) → API Gateway → scale-up Lambda →
`RunInstances` (spot, launch template) → instance registers as an **ephemeral**
self-hosted runner, takes exactly one job, scale-down Lambda reaps it.
Workflows target it with `runs-on: [self-hosted, kernel-builder, arm64]`.

Terraform: `workflows/aws-runners/` (not applied yet — needs GitHub App +
VPC fill-in).

## Sizing (speed-first)

`c8g.24xlarge` (96 vCPU) primary, `c8g.16xlarge` (64 vCPU) fallback,
`price-capacity-optimized` spot. Kernel compile saturates ~64–96 cores; the
serial deb stages dominate beyond that. Expected ~8–12 min per flavour warm,
~$0.30–0.50 per build at spot rates. Free tier (t4g.small, 2 vCPU/2 GB) is a
trap — slower than the local laptop VM by an order of magnitude.

## Speed levers (in priority order)

1. **Prebaked AMI** (Packer/EC2 Image Builder, recipe = env/Containerfile.noble
   content): saves ~10 min of apt per job. TODO.
2. **S3-warmed ccache**: sync /ccache at job start/end. SRU rebases reuse ~95%
   of objects → compile phase collapses to minutes. TODO.
3. **Spot interruption tolerance**: ephemeral runners + GitHub job retry.

## Guardrails (the part that costs money when missing)

- `runners_maximum_count = 4`, minimum running time 10 min, 5-min scale-down
  sweep — a hung build cannot hold a 96-vCPU box for a week.
- Billing alarm on the account. TODO.
- GitHub App key in SSM, never in repo.

## Measured baselines (local M1 Max VM, 8 vCPU, noble 6.8 generic-64k)

| Path | Wall | Notes |
|---|---|---|
| virtiofs tree, cold ccache | 3h38m | 155 min of sys time — syscall overhead |
| VM-local tree (rsync)      | TBD   | expected ~40–50 min cold, ~10–15 warm |

Local stays the iteration loop; Graviton spot is for CI/release/CVE respins.
