#!/usr/bin/env python3
"""Install the pinned official LLVM toolchain inside this project only."""
import argparse
import hashlib
from pathlib import Path
import platform
import subprocess
import urllib.request

# Resolve paths from the script, not the caller's working directory. Installation
# stays inside the ignored project toolchain directory and never replaces /usr/bin/clang++.
ROOT = Path(__file__).resolve().parents[1]
# Pin the archive name and digest together. This is content-integrity checking for
# one known official release, not a request for whichever LLVM version is newest.
VERSION = "21.1.8"
NAME = f"LLVM-{VERSION}-macOS-ARM64"
SHA256 = "b95bdd32a33a81ee4d40363aaeb26728a26783fcef26a4d80f65457433ea4669"
URL = f"https://github.com/llvm/llvm-project/releases/download/llvmorg-{VERSION}/{NAME}.tar.xz"


# Download if needed, verify the complete archive, then extract/build. --verify-only
# uses an existing local archive and deliberately performs neither download nor extraction.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() not in ("arm64", "aarch64"):
        parser.error("the pinned download is for macOS arm64; set STRICTCPP_LLVM to your LLVM 21 installation on other platforms")
    folder = ROOT / ".strictcpp/toolchains"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / f"{NAME}.tar.xz"
    if not archive.exists():
        if args.verify_only:
            parser.error("no downloaded archive to verify")
        # Keep an interrupted transfer separate from the archive name that verification
        # expects. A completed transfer is still untrusted until the SHA-256 check below succeeds.
        partial = archive.with_suffix(".partial")
        print(f"Downloading official LLVM {VERSION} (about 1.4 GiB) into {folder}", flush=True)
        urllib.request.urlretrieve(URL, partial)
        partial.rename(archive)
    # Hash in bounded chunks so verifying a roughly 1.4 GiB archive does not require
    # holding the archive in memory. Extraction is never attempted on a mismatched digest.
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != SHA256:
        parser.error("LLVM archive SHA-256 does not match the pinned official release digest; refusing extraction")
    print(f"Verified SHA-256: {SHA256}", flush=True)
    if args.verify_only:
        return
    # The pinned, verified distribution is the extraction trust boundary. Reuse an
    # existing developer prefix when its expected header is present, then let build.py
    # check the selected LLVM version and produce the local compiler components.
    if not (folder / NAME / "include/llvm/IR/Module.h").is_file():
        subprocess.run(["tar", "-xJf", str(archive), "-C", str(folder)], check=True)
    print(f"Installed LLVM at {folder / NAME}")
    subprocess.run(["python3", str(ROOT / "native/build.py")], check=True)


if __name__ == "__main__": main()
