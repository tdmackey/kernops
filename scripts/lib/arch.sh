#!/usr/bin/env bash
# Shared architecture helpers. Debian package architecture names are canonical.

gb200_arch_normalize() {
    case "${1:-}" in
        arm64|aarch64) echo arm64 ;;
        amd64|x86_64|x64) echo amd64 ;;
        *) echo "!! unsupported architecture: ${1:-<empty>} (use arm64 or amd64/x86_64)" >&2; return 2 ;;
    esac
}

gb200_arch_default_flavour() {
    local base="${1:?base required}"
    local arch="${2:?arch required}"
    case "$arch:$base" in
        arm64:*nvidia*) echo nvidia-64k ;;
        arm64:*)        echo generic-64k ;;
        amd64:*nvidia*) echo nvidia ;;
        amd64:*)        echo generic ;;
        *) echo "!! unsupported architecture: $arch" >&2; return 2 ;;
    esac
}

gb200_arch_file_grep() {
    case "${1:?arch required}" in
        arm64) echo 'PE32+.*Aarch64' ;;
        amd64) echo 'x86 boot executable|x86-64|bzImage' ;;
        *) echo "!! unsupported architecture: $1" >&2; return 2 ;;
    esac
}
