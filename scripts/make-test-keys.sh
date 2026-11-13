#!/usr/bin/env bash
# Make a self-signed key pair to exercise the signing flow. NOT for production:
# the private key is a plain file with no passphrase.
#
# Usage: make-test-keys.sh [directory]      (default: $WORK_ROOT/keys/test)
#
# Writes:
#   signing.key   private key (PEM)
#   signing.pem   certificate (PEM): give it to build-line.sh as TRUST_CERT
#   signing.der   the same certificate (DER): what a machine enrols for Secure Boot
#
# The certificate allows code signing, so the one key signs both the kernel
# image (Secure Boot) and kernel modules. A production setup uses separate
# keys that never leave a hardware module or a signing service.
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"

DIR="${1:-$WORK_ROOT/keys/test}"
[ ! -e "$DIR/signing.key" ] || { echo "!! $DIR/signing.key exists; remove the directory to make a new key" >&2; exit 1; }
mkdir -p "$DIR"; chmod 700 "$DIR"

cat > "$DIR/openssl.cnf" <<EOF
[ req ]
distinguished_name = dn
x509_extensions    = ext
prompt             = no
string_mask        = utf8only
[ dn ]
CN = $VENDOR TEST signing key (not for production)
[ ext ]
basicConstraints       = critical,CA:FALSE
keyUsage               = critical,digitalSignature
extendedKeyUsage       = codeSigning
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid
EOF
openssl req -new -x509 -newkey rsa:4096 -sha512 -nodes -days 365 -config "$DIR/openssl.cnf" \
    -keyout "$DIR/signing.key" -out "$DIR/signing.pem" 2>/dev/null
openssl x509 -in "$DIR/signing.pem" -outform DER -out "$DIR/signing.der"
chmod 600 "$DIR/signing.key"
echo "test key: $DIR"
openssl x509 -in "$DIR/signing.pem" -noout -subject -enddate -fingerprint -sha256 | sed 's/^/  /'
