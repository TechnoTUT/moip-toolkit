#!/bin/bash
# Installs the NDI runtime library (libndi.so.6) from the official NDI SDK for
# Linux, so moip-engine finds it on the system library path.
#
#   scripts/install-ndi-sdk.sh                  # install to /usr/local/lib (uses sudo)
#   PREFIX=/opt/ndi scripts/install-ndi-sdk.sh  # install to /opt/ndi/lib
#
# The SDK installer shows the NDI SDK license and asks you to accept it. Set
# NDI_ACCEPT_LICENSE=yes only if you have read and accepted it already (e.g.
# when provisioning more machines).
#
# Based on DistroAV's CI/libndi-get.sh
# (https://github.com/DistroAV/DistroAV/blob/master/CI/libndi-get.sh).
# Differences: the license is only accepted automatically on request, arm64 is
# supported, and the library directory is registered with the dynamic linker
# when needed (Fedora/RHEL do not search /usr/local/lib by default).
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
LIBDIR="$PREFIX/lib"
INSTALLER_NAME="Install_NDI_SDK_v6_Linux"
INSTALLER_URL="https://downloads.ndi.tv/SDK/NDI_SDK_Linux/$INSTALLER_NAME.tar.gz"

case "$(uname -m)" in
    x86_64) ARCH_GLOB="x86_64-linux-gnu" ;;
    aarch64 | arm64) ARCH_GLOB="aarch64-*" ;;
    *)
        echo "Unsupported architecture: $(uname -m) (x86_64 and aarch64 are supported)." >&2
        exit 1
        ;;
esac

export PATH="$PATH:/sbin:/usr/sbin"
for cmd in curl tar ldconfig; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Required command not found: $cmd" >&2
        exit 1
    fi
done

SUDO=""
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
fi

TMP="$(mktemp -d --tmpdir ndisdk.XXXXXXX)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

echo "Downloading $INSTALLER_URL ..."
curl -fL --retry 5 -o "$INSTALLER_NAME.tar.gz" "$INSTALLER_URL"
tar -xzf "$INSTALLER_NAME.tar.gz"

# The installer pages through the license and extracts "NDI SDK for Linux/"
# into the current directory once it is accepted.
if [[ "${NDI_ACCEPT_LICENSE:-}" == "yes" ]]; then
    # yes dies of SIGPIPE (status 141) once the installer stops reading;
    # under pipefail that would abort the script before libndi is copied.
    { yes || true; } | PAGER=cat sh "$INSTALLER_NAME.sh" >/dev/null
else
    PAGER="${PAGER:-less}" sh "$INSTALLER_NAME.sh"
fi

SDK="$TMP/NDI SDK for Linux"
if [[ ! -d "$SDK/lib" ]]; then
    echo "The SDK was not extracted (license not accepted?)" >&2
    exit 1
fi

# Pick the library directory for this CPU (on arm64 the directory name
# carries a board suffix, e.g. aarch64-rpi4-linux-gnueabi).
SRC=""
for dir in "$SDK"/lib/$ARCH_GLOB; do
    if ls "$dir"/libndi.so.6* >/dev/null 2>&1; then
        SRC="$dir"
        break
    fi
done
if [[ -z "$SRC" ]]; then
    echo "libndi.so.6 for $(uname -m) not found in the SDK. Available:" >&2
    ls "$SDK/lib" >&2
    exit 1
fi

echo "Installing libndi from $(basename "$SRC") to $LIBDIR ..."
$SUDO mkdir -p "$LIBDIR"
$SUDO cp -P "$SRC"/libndi.so* "$LIBDIR"/

# Make sure the dynamic linker searches $LIBDIR.
if ! ldconfig -v 2>/dev/null | grep -q "^$LIBDIR:"; then
    echo "Adding $LIBDIR to /etc/ld.so.conf.d/ndi-sdk.conf"
    echo "$LIBDIR" | $SUDO tee /etc/ld.so.conf.d/ndi-sdk.conf >/dev/null
fi
$SUDO ldconfig

if ldconfig -p | grep -q 'libndi\.so\.6 '; then
    echo "Installed: $(ldconfig -p | grep 'libndi\.so\.6 ' | head -1 | sed 's/^[[:space:]]*//')"
    strings "$LIBDIR/libndi.so.6" 2>/dev/null | grep -m1 '^NDI SDK LINUX' || true
else
    echo "libndi.so.6 was copied to $LIBDIR but the dynamic linker does not find it." >&2
    exit 1
fi
