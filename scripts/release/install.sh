#!/bin/sh
# Install the doppel-runtime host command (docs/specs/packaging.md): the release's binary for this machine, checked
# against the release's SHA256SUMS. It lands in /usr/local/bin (with sudo when that isn't writable), so the pair command
# on the same line finds it; without sudo, in ~/.local/bin. Docker 26 or later is the only other thing a runner needs.
#
#   curl -fsSL https://github.com/summationai/doppel-runtime/releases/latest/download/install.sh | sh
#
# DOPPEL_RUNTIME_VERSION=X.Y.Z picks a release (default: the latest); DOPPEL_INSTALL_DIR picks the folder.
# DOPPEL_HOST_RELEASES reads releases from elsewhere (a mirror; file:// in tests).
set -eu

releases="${DOPPEL_HOST_RELEASES:-https://github.com/summationai/doppel-runtime/releases}"
version="${DOPPEL_RUNTIME_VERSION:-}"

fail() { printf 'doppel-runtime install: %s\n' "$1" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) os=darwin ;;
  Linux) os=linux ;;
  *) fail "this is $(uname -s); a runner runs on a Mac or Linux" ;;
esac
case "$(uname -m)" in
  arm64 | aarch64) arch=arm64 ;;
  x86_64 | amd64) arch=amd64 ;;
  *) fail "this machine is $(uname -m); doppel-runtime is built for arm64 and x86_64" ;;
esac
asset="doppel-runtime_${os}_${arch}"
if [ -n "$version" ]; then
  base="$releases/download/v${version#v}"
else
  base="$releases/latest/download"
fi

if command -v curl >/dev/null 2>&1; then
  get() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  get() { wget -q "$1" -O "$2"; }
else
  fail "needs curl or wget"
fi
if command -v sha256sum >/dev/null 2>&1; then
  sum() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sum() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  fail "needs sha256sum or shasum to check the download"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
get "$base/$asset" "$tmp/$asset" || fail "couldn't download $asset from $base"
get "$base/SHA256SUMS" "$tmp/SHA256SUMS" || fail "couldn't download SHA256SUMS from $base"
want="$(awk -v name="$asset" '$2 == name || $2 == "*" name { print $1 }' "$tmp/SHA256SUMS")"
[ -n "$want" ] || fail "$asset isn't in the release's SHA256SUMS"
[ "$(sum "$tmp/$asset")" = "$want" ] || fail "$asset doesn't match the release's SHA256SUMS: not installed"
chmod 755 "$tmp/$asset"

dir="${DOPPEL_INSTALL_DIR:-/usr/local/bin}"
if [ -n "${DOPPEL_INSTALL_DIR:-}" ] || { [ -d "$dir" ] && [ -w "$dir" ]; }; then
  mkdir -p "$dir"
  mv "$tmp/$asset" "$dir/doppel-runtime"
elif command -v sudo >/dev/null 2>&1; then
  printf 'Installing doppel-runtime into %s (sudo asks for your password)\n' "$dir"
  sudo mkdir -p "$dir"
  sudo install -m 755 "$tmp/$asset" "$dir/doppel-runtime"
else
  dir="$HOME/.local/bin"
  mkdir -p "$dir"
  mv "$tmp/$asset" "$dir/doppel-runtime"
  case ":$PATH:" in
    *":$dir:"*) ;;
    *) printf 'Add %s to your PATH to run doppel-runtime.\n' "$dir" ;;
  esac
fi

printf 'Installed %s at %s/doppel-runtime.\n' "$("$dir/doppel-runtime" version)" "$dir"
printf 'Next: doppel-runtime pair <code> --bff <Doppelwerk'"'"'s address>  (the code: Doppelwerk, Runners, Add a runner)\n'
