#!/usr/bin/env python3
"""Run native examples with an ASan/UBSan-instrumented checking runtime.

The host clang++ must be able to consume the selected toolchain's LLVM bitcode.
This verifies the runtime on these examples, not the compiler's soundness.
"""
from pathlib import Path
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]


# Instrument the checker implementation itself with ASan/UBSan, then relink the
# already checked bitcode against that runtime. This tests runtime implementation
# errors on the examples; it does not prove the compiler's source safety contract.
def main():
    destination = ROOT / 'build/native-sanitizers'
    destination.mkdir(parents=True, exist_ok=True)
    # Use a host compiler with sanitizer runtimes installed. It must also understand
    # the selected LLVM bitcode version; an incompatible host/toolchain pair is a real error.
    compiler = os.environ.get('STRICTCPP_SANITIZER_CXX', 'clang++')
    flags = ['-std=c++17', '-O1', '-g', '-fsanitize=address,undefined',
             '-fno-sanitize-recover=all', '-fno-omit-frame-pointer']
    runtime = destination / 'runtime.o'
    subprocess.run([compiler, *flags, '-c', str(ROOT / 'native/runtime.cpp'), '-o', str(runtime)], check=True)
    # Positive examples must complete with zero; negative examples must exit 86 for the
    # specific intended Strict C++ reason. Checking the reason prevents an earlier unrelated
    # false positive from being mistaken for a successful negative test.
    examples = [('valid', 0, None), ('heap_array', 0, None), ('type_confusion', 86, 'type-mismatch'),
                ('vector_lifetime', 86, 'expired-object'), ('replacement_allocator', 0, None),
                ('union_members', 0, None), ('union_lifetime', 86, 'union-inactive-member'),
                ('character_alias', 86, 'type-mismatch'), ('strict', 0, None)]
    for name, expected, diagnostic in examples:
        folder = destination / name
        folder.mkdir(parents=True, exist_ok=True)
        source = ROOT / 'examples' / (name + '.cpp')
        # The strict example must pass source admission before its IR is reused. The other
        # examples intentionally exercise broader experimental backend behavior.
        policy = ['--sc-strict'] if name == 'strict' else []
        subprocess.run([str(ROOT / 'bin/strict++'), '--sc-quiet', *policy, '--sc-emit-dir', str(folder),
                        str(source), '-o', str(folder / 'plain')], check=True)
        executable = folder / 'sanitized'
        subprocess.run([compiler, *flags, str(folder / ('0-' + name + '.strict.bc')),
                        str(runtime), '-pthread', '-o', str(executable)], check=True)
        # Bound execution time and capture diagnostics so hangs, sanitizer reports, missing
        # checker failures and incorrect success exits are all visible to the verification command.
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
        if result.returncode != expected:
            raise RuntimeError(f'{name}: expected {expected}, got {result.returncode}\n{result.stderr}')
        if 'ERROR: AddressSanitizer' in result.stderr or 'runtime error:' in result.stderr:
            raise RuntimeError(f'{name}: sanitizer failure\n{result.stderr}')
        if diagnostic and 'strictcpp: ' + diagnostic not in result.stderr:
            raise RuntimeError(f'{name}: missing {diagnostic} diagnostic\n{result.stderr}')
        print(f'{name}: ASan/UBSan runtime check passed, exit {result.returncode}', flush=True)


if __name__ == '__main__': main()
