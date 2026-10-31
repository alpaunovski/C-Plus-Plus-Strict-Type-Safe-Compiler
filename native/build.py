#!/usr/bin/env python3
"""Build the native pass against one specific LLVM installation."""
from pathlib import Path
import os
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


# Try the explicitly configured prefix first, followed by locally installed prefixes.
# The native backend needs development headers and opt, not merely a runnable system
# clang++. A configured prefix that fails these checks can fall back to a local candidate.
def toolchain():
    configured = os.environ.get("STRICTCPP_LLVM")
    candidates = ([Path(configured)] if configured else []) + list((ROOT / ".strictcpp/toolchains").glob("LLVM-*/"))
    for candidate in candidates:
        if (candidate / "bin/opt").is_file() and (candidate / "include/llvm/IR/Module.h").is_file():
            return candidate.resolve()
    raise RuntimeError("set STRICTCPP_LLVM to an LLVM installation with development headers and opt")


# Build the pass plugin, frontend plugin and trusted runtime as separate artifacts.
# Plugins use the selected LLVM C++ ABI; the runtime is compiled without user source
# flags/macros so replacement allocators and instrumentation cannot recursively affect it.
def build():
    llvm = toolchain()
    output = ROOT / "build/native"
    output.mkdir(parents=True, exist_ok=True)
    version = subprocess.check_output([str(llvm / "bin/llvm-config"), "--version"], text=True).strip()
    if version.split('.')[0] != '21':
        raise RuntimeError(f"the native backend currently targets LLVM 21, found {version}")
    # A lightweight installation fingerprint prevents reusing plugins after switching
    # LLVM prefixes or replacing clang++. Source/header mtimes below separately invalidate
    # each artifact; this cache is not a content-addressed build system.
    stamp = {"toolchain": str(llvm), "version": version, "compiler_mtime": (llvm / 'bin/clang++').stat().st_mtime_ns}
    stamp_path = output / 'toolchain.json'
    try:
        changed = json.loads(stamp_path.read_text()) != stamp
    except (OSError, ValueError):
        changed = True
    plugin = output / ("StrictCppPass.dylib" if sys.platform == "darwin" else "StrictCppPass.so")
    runtime = output / "runtime.o"
    frontend = output / ("StrictCppFrontend.dylib" if sys.platform == "darwin" else "StrictCppFrontend.so")
    compiler = str(llvm / "bin/clang++")
    sysroot = []
    # The LLVM archive does not supply the Apple platform SDK. Ask the active Xcode
    # command-line tools for its SDK rather than assuming a machine-specific absolute path.
    if sys.platform == "darwin":
        sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
        sysroot = ["-isysroot", sdk]
    # Resolve LLVM symbols from the loading opt/Clang process on macOS. The plugin must
    # therefore be loaded by its matching toolchain, not an unrelated system LLVM binary.
    dependencies = [ROOT / "native/pass.cpp"]
    if changed or not plugin.exists() or any(p.stat().st_mtime > plugin.stat().st_mtime for p in dependencies):
        command = [compiler, *sysroot, "-std=c++17", "-O2", "-fPIC", "-fno-rtti", "-fno-exceptions",
                   "-I", str(llvm / "include"), "-shared", str(dependencies[0]), "-o", str(plugin)]
        if sys.platform == "darwin":
            command += ["-Wl,-undefined,dynamic_lookup"]
        subprocess.run(command, check=True)
    # The source admission header participates in frontend cache invalidation because
    # changing its allowlist changes which user programs may be compiled in strict mode.
    dependencies = [ROOT / "native/frontend.cpp", ROOT / "native/strict_profile.h"]
    if changed or not frontend.exists() or any(p.stat().st_mtime > frontend.stat().st_mtime for p in dependencies):
        command = [compiler, *sysroot, "-std=c++17", "-O2", "-fPIC", "-fno-rtti", "-fno-exceptions",
                   "-I", str(llvm / "include"), "-shared", str(dependencies[0]), "-o", str(frontend)]
        if sys.platform == "darwin": command += ["-Wl,-undefined,dynamic_lookup"]
        subprocess.run(command, check=True)
    # Only the runtime sees the shared ABI header directly. Any ABI layout change also
    # requires updating the pass's manually constructed LLVM descriptor types.
    dependencies = [ROOT / "native/runtime.cpp", ROOT / "native/abi.h"]
    if changed or not runtime.exists() or any(p.stat().st_mtime > runtime.stat().st_mtime for p in dependencies):
        subprocess.run([compiler, *sysroot, "-std=c++17", "-O2", "-g", "-fPIC", "-pthread", "-Wall", "-Wextra",
                        "-c", str(dependencies[0]), "-o", str(runtime)], check=True)
    # Write the fingerprint only after every required build succeeds. Failed compilation
    # must not make a partially refreshed artifact set appear current on the next invocation.
    stamp_path.write_text(json.dumps(stamp, indent=2) + '\n')
    return llvm, plugin, runtime, sysroot


if __name__ == "__main__":
    llvm, plugin, runtime, _ = build()
    print(f"LLVM: {llvm}\nPass: {plugin}\nRuntime: {runtime}")
