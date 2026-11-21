#!/usr/bin/env bash
set -euo pipefail

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
suffix=
[[ $(uname -m) == aarch64 ]] && suffix=_arm64
curl -fLsS --retry 3 \
  "https://github.com/shivammathur/php-builder/releases/download/7.1/php_7.1-dbgsym%2Bubuntu22.04${suffix}.tar.xz" \
  -o "$work/package.tar.xz"

# Ubuntu 22.04's zstd 1.4.8 is the oldest version used by these builds.
docker run --rm -v "$work:/validation" ubuntu:22.04 bash -euo pipefail -c '
  apt-get update -qq
  apt-get install -y --no-install-recommends zstd xz-utils > /dev/null
  cd /validation
  zstd -V
  free -m || true
  xz -dc package.tar.xz | zstd -22 -T0 --ultra > package.tar.zst
  zstd -t package.tar.zst
  original=$(xz -dc package.tar.xz | sha256sum | cut -d " " -f1)
  repacked=$(zstd -dc package.tar.zst | sha256sum | cut -d " " -f1)
  test "$original" = "$repacked"
  printf "Verified PHP debug archive content SHA256: %s\n" "$repacked"
  tar --use-compress-program=zstd -tf package.tar.zst | wc -l
  ls -lh package.tar.xz package.tar.zst
'
