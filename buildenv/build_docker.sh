#!/usr/bin/env bash
#
# Builds the four toolchain images the main build compiles inside, and pushes them to GHCR.
#
# NAMESPACE. This used to hardcode ghcr.io/nyamisty, so in any fork every `docker push` was denied
# -- a fork's GITHUB_TOKEN cannot write to another account's packages -- and the workflow failed
# after spending several minutes building four architectures under QEMU. It now derives the
# namespace from whoever owns the repository, which GitHub Actions supplies as
# GITHUB_REPOSITORY_OWNER, so it works in a fork without editing. Override with GHCR_NAMESPACE to
# push somewhere else. GHCR requires lowercase, so the value is lowercased.
#
#   bash build_docker.sh                     # in CI: pushes to the repo owner's namespace
#   GHCR_NAMESPACE=someone bash build_docker.sh
#
# NOTE: this only changes where images are PUBLISHED. Nothing consumes them: build.yml,
# build_image.yml and docker/Dockerfile build inside ghcr.io/ben-diehlci/altserver_builder_alpine_*
# PINNED BY DIGEST. A rebuild here is NOT a copy of those: it re-downloads corecrypto from Apple
# and clones cpprestsdk (archived upstream 2026-05) and libzip master at whatever they are today,
# on EOL Alpine 3.15, so it yields a different toolchain. To stop depending on another account,
# mirror the pinned images instead (docker pull <image>@sha256:..., tag, push to your namespace).

set -euo pipefail

NS="${GHCR_NAMESPACE:-${GITHUB_REPOSITORY_OWNER:-}}"
if [ -z "$NS" ]; then
    echo "No namespace: set GHCR_NAMESPACE, or run where GITHUB_REPOSITORY_OWNER is set." >&2
    exit 1
fi
NS="$(printf '%s' "$NS" | tr '[:upper:]' '[:lower:]')"

echo "Publishing toolchain images to ghcr.io/${NS}/"

build_and_push() {
    local base="$1" tag="$2"
    local image="ghcr.io/${NS}/altserver_builder_alpine_${tag}"
    echo "==> ${image}  (from ${base})"
    docker build --build-arg "IMAGE=${base}" -t "${image}" .
    docker push "${image}"
}

# amd64 FIRST, deliberately. It is the only one CI consumes (build_image.yml's BUILDER arg) and
# the only one that builds natively on a GitHub runner. The other three run under QEMU: slow, and
# the likeliest to fail. With `set -e` above, building the fragile ones first would mean a QEMU
# failure costs you the image you actually needed -- which is worse than the unguarded script
# this replaced, where a failed armv7 simply fell through to amd64. Native first, emulated after.
build_and_push amd64/alpine:3.15   amd64
build_and_push arm64v8/alpine:3.15 aarch64
build_and_push arm32v7/alpine:3.15 armv7
build_and_push i386/alpine:3.15    i386

echo
echo "Done. To actually BUILD against these instead of the digest-pinned ben-diehlci images, repoint"
echo "(by digest -- docker inspect --format '{{index .RepoDigests 0}}' <image>):"
echo "  .github/workflows/build.yml       (the four matrix entries)"
echo "  docker/Dockerfile                 (ARG BUILDER_AMD64 / BUILDER_ARM64)"
echo "  README.md                         (the docker run example)"
