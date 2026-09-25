#!/usr/bin/env python3
"""Check that every toolchain image is pinned, and that each architecture gets its own.

WHY THIS EXISTS. The shipped binary is compiled and statically linked (LibreSSL, cpprestsdk,
libzip, corecrypto) inside another account's toolchain images, ghcr.io/ben-diehlci/
altserver_builder_alpine_<arch>. Two ways that silently changes what ships:

  * A mutable tag. `:latest` can be re-pushed at any time; the build then compiles against a
    different toolchain with no commit here. So every reference must carry an @sha256 digest, and
    the same architecture must resolve to the same digest in build.yml and docker/Dockerfile --
    otherwise the CI binary and the image binary come from different toolchains.
  * The wrong architecture. The images are single-arch manifests, so nothing but these names
    decides which toolchain a platform gets. docker/Dockerfile defaulted to the amd64 toolchain,
    which on a Raspberry Pi died with "exec format error" or, with QEMU registered, produced an
    arm64-labelled image carrying an x86-64 AltServer. The in-image ELF check catches that only
    when an image is actually built; this catches it on every push, in milliseconds.

Stdlib only.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, ".github", "workflows", "build.yml")
IMAGE = os.path.join(ROOT, ".github", "workflows", "build_image.yml")
DOCKERFILE = os.path.join(ROOT, "docker", "Dockerfile")

REF = re.compile(r"^ghcr\.io/[a-z0-9._-]+/altserver_builder_alpine_(amd64|aarch64|armv7|i386)"
                 r"@sha256:[0-9a-f]{64}$")
# Dockerfile ARG suffix / build.yml matrix label -> builder image suffix
DOCKER_ARCH = {"AMD64": "amd64", "ARM64": "aarch64"}


def code_lines(path):
    return [l for l in open(path).read().splitlines() if not l.lstrip().startswith("#")]


def main():
    failures = []
    refs = []  # (where, expected_arch, ref)

    for l in code_lines(BUILD):
        for arch, ref in re.findall(r'"arch":"([a-z0-9]+)","builder":"([^"]+)"', l):
            refs.append(("build.yml matrix %s" % arch, arch, ref))
    for l in code_lines(DOCKERFILE):
        m = re.match(r"\s*ARG\s+BUILDER_([A-Z0-9]+)=(\S*)\s*$", l)
        if m:
            refs.append(("Dockerfile BUILDER_%s" % m.group(1), DOCKER_ARCH.get(m.group(1), "?"),
                         m.group(2)))

    if not any(w.startswith("build.yml") for w, _, _ in refs):
        failures.append("build.yml: no builder entries found in matrix_setup -- did the format change?")
    if sorted(w for w, _, _ in refs if w.startswith("Dockerfile")) != [
            "Dockerfile BUILDER_AMD64", "Dockerfile BUILDER_ARM64"]:
        failures.append("docker/Dockerfile must define ARG BUILDER_AMD64 and ARG BUILDER_ARM64 "
                        "(one toolchain per TARGETARCH stage)")

    digest_by_arch = {}
    for where, want, ref in refs:
        m = REF.match(ref)
        if not m:
            failures.append("%s = %r is not a digest-pinned altserver_builder_alpine_<arch> "
                            "reference (a tag can be re-pushed under you)" % (where, ref))
            continue
        before = len(failures)
        if m.group(1) != want:
            failures.append("%s points at the %s toolchain: that platform would get a binary "
                            "for the wrong architecture" % (where, m.group(1)))
        digest = ref.split("@", 1)[1]
        prev = digest_by_arch.setdefault(m.group(1), (where, digest))
        if prev[1] != digest:
            failures.append("%s and %s pin DIFFERENT %s toolchains; CI and the image would ship "
                            "binaries from different compilers and libraries"
                            % (prev[0], where, m.group(1)))
        if len(failures) == before:
            print("ok    %-28s %s@%s..." % (where, m.group(1), digest[7:19]))

    # build_image.yml: a single BUILDER forces one toolchain onto every listed platform.
    text = "\n".join(code_lines(IMAGE))
    plat = re.search(r"^\s*platforms:\s*(\S+)", text, re.M)
    platforms = plat.group(1).split(",") if plat else []
    if re.search(r"^\s*BUILDER=", text, re.M) and len(platforms) != 1:
        failures.append("build_image.yml passes a BUILDER build-arg for %d platforms; it overrides "
                        "the Dockerfile's per-TARGETARCH choice and mislabels all but one"
                        % len(platforms))
    else:
        print("ok    %-28s platforms=%s, no forcing BUILDER" % ("build_image.yml", ",".join(platforms)))

    if failures:
        print("\n" + "\n".join("FAIL: " + f for f in failures))
        return 1
    print("\nEvery toolchain is pinned by digest, and each architecture gets its own.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
