#!/usr/bin/env bash
# One-time (idempotent) setup of the local build environment on macOS:
#   podman VM (applehv, native arm64) + builder container images.
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"
cd "$(dirname "$0")/.."

MACHINE=kernel-builder
CPUS=8
MEM_MB=20480
DISK_GB=150

if ! podman machine inspect "$MACHINE" >/dev/null 2>&1; then
    echo ">> creating podman machine '$MACHINE' (${CPUS} cpu / ${MEM_MB} MiB / ${DISK_GB} GB)"
    podman machine init \
        --cpus "$CPUS" --memory "$MEM_MB" --disk-size "$DISK_GB" \
        --volume "$WORK_ROOT:$WORK_ROOT" \
        "$MACHINE"
fi

if [ "$(podman machine inspect "$MACHINE" --format '{{.State}}')" != "running" ]; then
    echo ">> starting podman machine '$MACHINE'"
    podman machine start "$MACHINE"
fi

# Make this machine the default connection so plain `podman` targets it.
podman system connection default "$MACHINE"

for series in noble resolute; do
    echo ">> building builder image: kernel-builder:$series"
    podman build -t "localhost/kernel-builder:$series" -f "env/Containerfile.$series" env/
done

# ccache named volumes live on the VM's own disk (fast), not virtiofs.
for series in noble resolute; do
    podman volume create --ignore "kernel-ccache-$series" >/dev/null
done

echo ">> done. try: ./scripts/build-kernel.sh $WORK_ROOT/noble generic"
