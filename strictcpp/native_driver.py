"""Compile unchanged C++ through an early LLVM instrumentation pass."""
from __future__ import annotations
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from native.build import build
from . import __version__
from .policy import PROFILE, validate_strict


# Write build evidence separately from human-readable compiler output. A strict
# frontend rejection can include diagnostics before any translation-unit IR report
# exists; successful final status is recorded only after the requested build stage.
def write_report(options, reports, status, diagnostics=None):
    if not options['report']:
        return
    data = {'version': __version__, 'backend': 'llvm-native', 'status': status,
            'mode': 'strict' if options.get('strict') else 'experimental', 'translation_units': reports}
    if options.get('strict'):
        data['profile'] = PROFILE
    if diagnostics:
        data['diagnostics'] = diagnostics
    Path(options['report']).write_text(json.dumps(data, indent=2) + '\n')


# Coordinate the four native stages: semantic Clang plugin, early LLVM pass,
# object generation, and one final link with the uninstrumented checking runtime.
# The original source is the frontend input; this path performs no source rewriting.
def compile_native(options, args, temporary):
    from .driver import DriverError, execute, source_arguments, analysis_arguments, validate, TAKES_VALUE, SOURCES
    # Validate public options before invoking tools. Strict policy is an additional
    # closed allowlist, not a replacement for the shared job-graph restrictions.
    validate(args, ast=False)
    validate_strict(options, args)
    if options["clang"] != "clang++":
        raise DriverError("--sc-clang/STRICTCPP_CLANG applies to the AST backend; set STRICTCPP_LLVM for a matching native toolchain")
    if options["unknown"]:
        raise DriverError("native provenance checks cannot be disabled with --sc-allow-untracked; use the AST backend for that experiment")
    # Both plugins must be built against the selected LLVM installation. Mixing host
    # Clang headers, a different opt, and cached plugins can break the compiler ABI.
    try:
        llvm, plugin, runtime, sysroot = build()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise DriverError(f"cannot build native backend: {exc}") from exc
    compiler = str(llvm / "bin/clang++")
    frontend_plugin = plugin.parent / ("StrictCppFrontend.dylib" if sys.platform == "darwin" else "StrictCppFrontend.so")
    opt = str(llvm / "bin/opt")
    if not args or (not source_arguments(args) and any(a in args for a in ("--version", "-v", "-dumpmachine", "-print-resource-dir", "-print-search-dirs"))):
        return execute([compiler, *sysroot, *args]).returncode
    if "-E" in args:
        return execute([compiler, *sysroot, *args]).returncode
    if not any(a.startswith("-std=") for a in args):
        args = ["-std=c++17", *args]
    sources = source_arguments(args)
    # Do not accept hidden object/library inputs in coverage-rejection mode. There is
    # no verified coverage manifest for existing objects, archives, raw IR or linker scripts.
    if options["require_coverage"]:
        skip_value = False
        for arg in args:
            if skip_value:
                skip_value = False
                continue
            if arg.startswith(('-Wl,', '-Xlinker', '-l', '-framework', '-weak_framework')):
                raise DriverError(f"coverage validation does not support linker-controlled input: {arg}")
            if arg in TAKES_VALUE:
                skip_value = True
                continue
            if arg == '-' or (not arg.startswith('-') and Path(arg).suffix not in SOURCES):
                raise DriverError("strict link validation of prebuilt objects/libraries and other non-source inputs is not implemented; compile source inputs together for coverage rejection")
        if not sources:
            raise DriverError("coverage validation requires source compilation units")
    indexes = {i for i, _ in sources}
    # Use O1 only to obtain useful frontend type metadata, with LLVM optimization passes
    # disabled. The requested optimization level is applied after instrumentation, so
    # ordinary optimization does not erase the checked operations before we inspect them.
    common = analysis_arguments(args, indexes)
    common = [a for a in common if not re.match(r"^-O[0-3szgfast]*$", a) and a != "-fsyntax-only"]
    level = "O0"
    for a in args:
        if re.match(r"^-O[0-3sz]$", a):
            level = a[1:]
    syntax_only = "-fsyntax-only" in args
    assembly = "-S" in args
    compile_only = "-c" in args or assembly
    no_link = syntax_only or compile_only
    if no_link and len(sources) > 1 and "-o" in args:
        raise DriverError("cannot use one -o output for several compilation units")
    output = args[args.index("-o") + 1] if "-o" in args else None
    if len(sources) > 1 and '-MF' in args:
        raise DriverError("use a separate dependency file per source compilation")
    destination = Path(options["emit"]).resolve() if options["emit"] else temporary
    destination.mkdir(parents=True, exist_ok=True)
    reports = []
    generated = {}
    # Compile translation units independently. Stable numbered filenames avoid collisions
    # when different source directories contain the same stem, and make emitted IR inspectable.
    for n, (index, source) in enumerate(sources):
        raw = temporary / f"unit-{n}.bc"
        checked = destination / f"{n}-{source.stem}.strict.bc"
        object_path = temporary / f"unit-{n}.o"
        frontend = [compiler, *sysroot, *common, "-O1", "-g", "-fstandalone-debug", "-fstrict-aliasing",
                    "-Xclang", "-load", "-Xclang", str(frontend_plugin),
                    "-Xclang", "-disable-llvm-passes", "-emit-llvm", "-c", str(source), "-o", str(raw)]
        # Dependency files must describe the user-visible target rather than unit-N.bc.
        # Quoted Make targets preserve spaces/special characters in normal dependency generation.
        dependency_mode = '-MMD' if '-MMD' in args else ('-MD' if '-MD' in args else None)
        if dependency_mode:
            final_target = Path(output) if output else (Path(source.stem + ('.s' if assembly else '.o')) if compile_only else Path('a.out'))
            dependency_path = args[args.index('-MF') + 1] if '-MF' in args else str(final_target.with_suffix('.d'))
            frontend += [dependency_mode, '-MF', dependency_path]
            if '-MP' in args: frontend += ['-MP']
            targets = []
            for i, arg in enumerate(args):
                if arg in ('-MT','-MQ') and i + 1 < len(args): targets += args[i:i + 2]
            frontend += targets or ['-MQ', str(final_target)]
        # The frontend metadata strategy uses O1 even for O0 requests. Undefining this macro
        # reduces one observable difference, but does not reproduce every Clang option/macro behavior.
        if level == "O0":
            frontend += ["-U__OPTIMIZE__"]
        # Private plugin arguments enable the original-AST allowlist. These diagnostics are
        # forced after user warning controls so missing returns/unsequenced mutations cannot
        # be admitted merely by disabling a warning category.
        if options.get("strict"):
            frontend += ["-Xclang", "-plugin-arg-strictcpp-lifetimes", "-Xclang", PROFILE,
                         "-Werror=return-type", "-Werror=unsequenced"]
        result = execute(frontend, capture_output=True, text=True)
        if result.returncode or not options["quiet"]:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode:
            if options.get("strict"):
                write_report(options, reports, 'rejected', result.stderr)
            return result.returncode
        if options.get("strict") and len(re.findall(r"^strictcpp-strict: " + PROFILE + r" accepted$", result.stderr, re.MULTILINE)) != 1:
            raise DriverError(f"{source}: missing or ambiguous strict source-gate report; refusing unchecked output")
        # Require exactly one anchored report from each stage. Substring matching could
        # mistake a user diagnostic for a report; absent/ambiguous evidence must fail closed.
        front_summaries = re.findall(r"^strictcpp-front: (\d+) placement hooks, (\d+) semantic sites deferred$", result.stderr, re.MULTILINE)
        if len(front_summaries) != 1:
            raise DriverError(f"{source}: missing or ambiguous semantic instrumentation report; refusing unchecked output")
        front_deferred = int(front_summaries[0][1])
        # Run the checker before the selected optimization pipeline and verify IR on both
        # sides of optimization. A structurally invalid transformed module never reaches linking.
        result = execute([opt, "-load-pass-plugin", str(plugin),
                          f"-passes=strictcpp,verify,default<{level}>,verify", str(raw), "-o", str(checked)],
                         capture_output=True, text=True)
        if result.returncode:
            print(result.stderr, file=sys.stderr, end="")
            return result.returncode
        summaries = re.findall(r"^strictcpp-native: (\d+) functions, (\d+) accesses, (\d+) object sites, (\d+) known coverage gaps$", result.stderr, re.MULTILINE)
        if len(summaries) != 1:
            raise DriverError(f"{source}: missing or ambiguous native instrumentation report; refusing unchecked output")
        counts = dict(zip(("functions", "accesses", "object_sites", "known_gaps"), map(int, summaries[0])))
        foreign_lines = [line.split(": ", 1)[1] for line in result.stderr.splitlines() if line.startswith("strictcpp-native-foreign: ")]
        if len(foreign_lines) != 1:
            raise DriverError(f"{source}: missing or ambiguous foreign-boundary report; refusing unchecked output")
        try:
            foreign = json.loads(foreign_lines[0])
        except ValueError as exc:
            raise DriverError(f"{source}: malformed foreign-boundary report") from exc
        if not isinstance(foreign, list) or not all(isinstance(symbol, str) for symbol in foreign):
            raise DriverError(f"{source}: malformed foreign-boundary report")
        # Coverage counters describe the implemented analysis, not a soundness proof.
        # Foreign/deferred sites are preserved in JSON even when policy rejects the build.
        counts["frontend_deferred"] = front_deferred
        report = {"source": str(source), "backend": "llvm-native", "scope": "all IR definitions, including headers, templates, and expanded macros",
                  "source_policy": PROFILE if options.get("strict") else "experimental",
                  "counts": counts, "foreign_symbols": foreign,
                  "limitations": ["foreign binary code and callbacks are trust boundaries", "object-model and source-type information can be lost before IR; no full soundness claim"]}
        reports.append(report)
        if not options["quiet"]:
            for line in result.stderr.splitlines():
                if not line.startswith("strictcpp-native-foreign:"):
                    print(line, file=sys.stderr)
            print("strictcpp: native IR coverage includes headers/macros; foreign binaries remain trust boundaries", file=sys.stderr)
            if foreign:
                print(f"strictcpp: {len(foreign)} foreign binary call targets (listed in --sc-report)", file=sys.stderr)
        if options["require_coverage"] and (counts.get("known_gaps", 0) or front_deferred or foreign):
            write_report(options, reports, 'rejected')
            raise DriverError(f"{source}: incomplete coverage: {counts.get('known_gaps', 0)} native gaps, {front_deferred} deferred frontend sites, {len(foreign)} foreign call targets")
        # Syntax-only requests still run the semantic and IR checks. They intentionally stop
        # before object generation; strict-v1 excludes this job form in its driver allowlist.
        if syntax_only:
            continue
        if assembly:
            target = Path(output) if output else Path(source.stem + ".s")
            command = [compiler, *sysroot, "-S", str(checked), "-o", str(target)]
        else:
            command = [compiler, *sysroot, "-c", str(checked), "-o", str(object_path)]
        status = execute(command).returncode
        if status:
            return status
        if compile_only and not assembly:
            target = Path(output) if output else Path(source.stem + ".o")
            shutil.copyfile(object_path, target)
        generated[index] = object_path
    if no_link:
        write_report(options, reports, 'accepted')
        return 0
    # Replace source operands with their instrumented objects while retaining the public
    # link order. Dependency-generation options belong to compilation and must not leak
    # into this final job. The runtime is appended once for the complete executable.
    linked = []
    skip = False
    for i, arg in enumerate(args):
        if skip: skip = False; continue
        if arg in ('-MF','-MT','-MQ'): skip = True; continue
        if arg in ('-MD','-MMD','-MP'): continue
        linked.append(str(generated.get(i, arg)))
    status = execute([compiler, *sysroot, *linked, str(runtime), "-pthread"]).returncode
    write_report(options, reports, 'accepted' if status == 0 else 'rejected')
    return status
