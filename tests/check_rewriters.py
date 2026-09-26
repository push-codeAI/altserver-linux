#!/usr/bin/env python3
"""Run every build-time rewriter over its real inputs, and compile what a current toolchain would.

WHY THIS EXISTS. The vendored sources are patched at build time by makefiles/**/rewrite_*.py, and
each rewriter exits non-zero when a pattern stops matching. But that exit only happens inside the
build job, after checkout, QEMU setup and roughly nine minutes of emulated aarch64 compilation --
and the shipped build runs on a frozen Alpine 3.15 toolchain, so it cannot notice that the
rewritten sources no longer compile anywhere newer. Both failures were real:

  * AltSign's Archiver.cpp uses std::vector without including <vector>. Alpine 3.15's libstdc++ 10
    included it transitively; libstdc++ 13 and 14 do not, so every newer toolchain fails with
    "no member named 'vector' in namespace 'std'".
  * A rewriter whose guard fails leaves nothing behind for this job to see: the pattern mismatch is
    only reported by the build, and a re-run of an incremental build used to compile the empty
    output it left instead (fixed with .DELETE_ON_ERROR in makefiles/main.mak).

So this runs the three source rewriters over the exact file sets the makefiles feed them (a guard
that fails fails here, in seconds), then compiles the rewritten Archiver.cpp -- whose include
closure is only the C++ standard library, the shims, minizip and zlib -- with whatever compiler
the runner has. Any new patch added to a rewriter (for example to WirelessConnection.cpp) is
exercised by the first half automatically.

Needs the upstream_repo submodule:  git submodule update --init --depth 1 upstream_repo
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UP = os.path.join(ROOT, "upstream_repo")
MK = os.path.join(ROOT, "makefiles")

# Mirrors the makefiles: main_orifiles = $(wildcard $(main_srcroot)/*.*), altsign_orifiles =
# $(wildcard $(ALTSIGN_ROOT)/*.*), ldid_orifiles = ldid.cpp lookup2.c.
JOBS = [
    ("AltServer_patched", os.path.join(MK, "rewrite_altserver_source.py"),
     sorted(glob.glob(os.path.join(UP, "AltServer", "*.*")))),
    ("AltSign_patched", os.path.join(MK, "AltSign-build", "rewrite_altsign_source.py"),
     sorted(glob.glob(os.path.join(UP, "AltSign", "*.*")))),
    ("ldid_patched", os.path.join(MK, "AltSign-build", "rewrite_ldid_source.py"),
     [os.path.join(UP, "ldid", "ldid.cpp"), os.path.join(UP, "ldid", "lookup2.c")]),
]


def main():
    strict = bool(os.environ.get("CI"))
    if not os.path.exists(os.path.join(UP, "AltServer", "AltServerApp.cpp")):
        msg = "upstream_repo is not checked out (git submodule update --init --depth 1 upstream_repo)"
        if strict:
            print("FAIL: " + msg + "\n      Refusing to skip under CI.")
            return 1
        print("SKIP: " + msg)
        return 0

    out = tempfile.mkdtemp(prefix="rewriters_")
    failures = 0
    try:
        for subdir, rewriter, files in JOBS:
            os.makedirs(os.path.join(out, subdir))
            bad = 0
            for f in files:
                p = subprocess.run([sys.executable, rewriter, f], capture_output=True)
                if p.returncode != 0:
                    bad += 1
                    print("FAIL  %s rejected %s:\n%s" % (os.path.basename(rewriter),
                          os.path.relpath(f, ROOT), p.stderr.decode("utf-8", "replace").rstrip()))
                    continue
                with open(os.path.join(out, subdir, os.path.basename(f)), "wb") as fh:
                    fh.write(p.stdout)
            print("%s  %-28s %d/%d files rewritten" % ("ok  " if not bad else "FAIL",
                  os.path.basename(rewriter), len(files) - bad, len(files)))
            failures += bad
        if failures:
            print("\nA rewriter no longer applies to the pinned sources; the build would fail the same way.")
            return 1

        # Compile the rewritten Archiver.cpp with the runner's own (current) compiler, using the
        # include flags AltSign.mak gives it. -mno-sse is omitted: it only matters on x86 codegen.
        cxx = os.environ.get("CXX") or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        if not cxx:
            if strict:
                print("FAIL: no C++ compiler on this runner; the toolchain canary did not run.")
                return 1
            print("SKIP: no C++ compiler for the toolchain canary")
            return 0
        argv = [cxx, "-std=c++17", "-fsyntax-only",
                "-I" + os.path.join(ROOT, "shims"), "-include", "windows_shim.h",
                "-I" + os.path.join(UP, "AltSign"),
                "-I" + os.path.join(UP, "AltSign", "Dependencies", "minizip"),
                "-I" + os.path.join(UP, "ldid"),
                os.path.join(out, "AltSign_patched", "Archiver.cpp")]
        p = subprocess.run(argv, capture_output=True, text=True)
        version = subprocess.run([cxx, "--version"], capture_output=True, text=True).stdout.splitlines()[:1]
        if p.returncode != 0:
            errors = [l for l in p.stderr.splitlines() if "error" in l][:5]
            print("FAIL  rewritten Archiver.cpp does not compile with %s:\n      %s"
                  % (" ".join(version), "\n      ".join(e.replace(out + "/", "") for e in errors)))
            if any("zlib.h" in e for e in errors):
                print("      (zlib.h missing on this runner: apt-get install zlib1g-dev)")
            return 1
        print("ok    rewritten Archiver.cpp compiles with %s" % " ".join(version))
    finally:
        shutil.rmtree(out, ignore_errors=True)

    print("\nAll rewriters apply, and the rewritten sources compile on a current toolchain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
