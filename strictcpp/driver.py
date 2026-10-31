"""Clang-compatible bootstrap compile driver (Python standard library only)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from . import __version__
from .instrument import InstrumentationError, Instrumenter

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
SOURCES = {".cpp", ".cc", ".cxx", ".C", ".c++"}
HELP = """strict++ [strictcpp options] [clang++ options] sources/objects...

Experimental compiler for unchanged C++ source. Native LLVM 21 is the default.
Run python3 native/bootstrap.py to install the pinned development toolchain.

  --sc-clang PATH             AST-backend compiler (or STRICTCPP_CLANG)
  --sc-backend native|ast     Native LLVM checks (default) or AST bootstrap
  --sc-emit-dir DIRECTORY     Keep native bitcode or AST-generated source
  --sc-report FILE            Write JSON instrumentation coverage report
  --sc-allow-untracked        AST-only: permit unknown storage (weaker checks)
  --sc-strict                 Enforce the small strict-v1 source/driver allowlist
  --sc-require-coverage       Reject reported gaps and foreign call targets
  --sc-require-main-coverage  Legacy alias for --sc-require-coverage
  --sc-quiet                  Hide instrumentation counts
  --sc-version               Print prototype version
  --sc-help                  Show this help

Native checks cover emitted IR in headers, templates, and macro expansions.
AST mode instruments input main files only and requires C++17+, RTTI, exceptions.
There are still known type/lifetime gaps: this is not a complete safety guarantee.
"""


# Errors raised by our policy/driver layer become a concise diagnostic and exit 2.
# Clang failures retain their own return codes instead of being recast as policy failures.
class DriverError(Exception):
    pass


# Pass argument vectors directly to subprocess: compiler options and paths must never
# be interpreted by a shell. Centralizing missing-tool errors keeps both backends consistent.
def execute(command, **kwargs):
    try:
        return subprocess.run(command, **kwargs)
    except FileNotFoundError as exc:
        raise DriverError(f"executable not found: {command[0]}") from exc


# Expand @files before classifying sources or validating policy. Otherwise unchecked
# inputs could hide behind a response file; the depth limit also prevents inclusion cycles.
def expand_response_files(args, depth=0):
    if depth > 10:
        raise DriverError("response files are nested too deeply")
    expanded = []
    for arg in args:
        if arg.startswith("@"):
            try:
                contents = Path(arg[1:]).read_text()
            except OSError as exc:
                raise DriverError(f"cannot read response file {arg[1:]}: {exc}") from exc
            expanded.extend(expand_response_files(shlex.split(contents), depth + 1))
        else:
            expanded.append(arg)
    return expanded


# Separate private --sc-* options from the Clang-compatible argument stream.
# Strict mode always enables coverage rejection; no later option can turn it off.
def parse(args):
    options = {"clang": os.environ.get("STRICTCPP_CLANG", "clang++"), "emit": None,
               "report": None, "unknown": False, "quiet": False, "require_coverage": False,
               "strict": False, "backend": "native"}
    forwarded = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--sc-help", "--help"):
            print(HELP)
            return None, None
        if arg == "--sc-version":
            print(f"strictcpp {__version__} (native LLVM and AST bootstrap backends)")
            return None, None
        if arg in ("--sc-clang", "--sc-emit-dir", "--sc-report", "--sc-backend"):
            if i + 1 == len(args):
                raise DriverError(f"missing value after {arg}")
            options[{"--sc-clang": "clang", "--sc-emit-dir": "emit", "--sc-report": "report", "--sc-backend": "backend"}[arg]] = args[i + 1]
            i += 2
            continue
        if arg == "--sc-allow-untracked":
            options["unknown"] = True
        elif arg == "--sc-quiet":
            options["quiet"] = True
        elif arg == "--sc-strict":
            options["strict"] = True
            options["require_coverage"] = True
        elif arg in ("--sc-require-main-coverage", "--sc-require-coverage"):
            options["require_coverage"] = True
        elif arg.startswith("--sc-"):
            raise DriverError(f"unknown compiler option: {arg}")
        else:
            forwarded.append(arg)
        i += 1
    return options, forwarded


# These tables describe the small portion of Clang argument structure this wrapper
# understands. An option value must not accidentally be classified as an input source.
# They are reconstruction rules, not a claim that arbitrary Clang job graphs are supported.
TAKES_VALUE = {"-o", "-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros",
               "-isysroot", "--sysroot", "-target", "--target", "-arch", "-x", "-D", "-U",
               "-L", "-l", "-Xclang", "-Xlinker", "-Xpreprocessor", "-mllvm", "-MF", "-MT", "-MQ"}
DROP_AST_VALUE = {"-o", "-L", "-l", "-Xlinker", "-MF", "-MT", "-MQ"}
DROP_AST_FLAG = {"-c", "-S", "-E", "-shared", "-dynamiclib", "-MD", "-MMD", "-MP"}


# Return original argument indexes as well as absolute source paths. The index lets
# compilation replace exactly that operand while preserving the surrounding link order.
def source_arguments(args):
    result = []
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg in TAKES_VALUE:
            skip = True
            continue
        if not arg.startswith("-") and Path(arg).suffix in SOURCES:
            result.append((index, Path(arg).resolve()))
    return result


# Build per-translation-unit frontend options by removing inputs, output destinations
# and link-only settings. Include paths and language-affecting flags remain attached to
# that source; callers add their own controlled output/analysis job.
def analysis_arguments(args, source_indexes):
    result = []
    i = 0
    while i < len(args):
        arg = args[i]
        if i in source_indexes or (not arg.startswith("-") and Path(arg).suffix in (".o", ".a", ".so", ".dylib")):
            i += 1
            continue
        if arg in DROP_AST_VALUE:
            i += 2
            continue
        if arg in DROP_AST_FLAG or arg.startswith(("-Wl,", "-l", "-L", "-MF", "-MT", "-MQ")):
            i += 1
            continue
        if arg in TAKES_VALUE:
            if i + 1 == len(args):
                raise DriverError(f"missing value after {arg}")
            result.extend(args[i:i + 2])
            i += 2
            continue
        result.append(arg)
        i += 1
    return result


# Reject language/runtime combinations and job-graph changes the wrapper cannot
# reconstruct safely. Native mode supports older source standards because its runtime
# is compiled separately; the AST helpers themselves require C++17, RTTI and exceptions.
def validate(args, *, ast=True):
    if ast and any(a in args for a in ("-fno-rtti", "-fno-exceptions")):
        raise DriverError("the prototype runtime requires RTTI and exceptions")
    for arg in args:
        if arg.startswith("-std="):
            value = arg.split("=", 1)[1].replace("gnu++", "c++")
            modern = ("c++17", "c++1z", "c++20", "c++2a", "c++23", "c++2b", "c++26", "c++2c", "c++2d")
            legacy = ("c++98", "c++03", "c++11", "c++0x", "c++14", "c++1y")
            if value not in modern and (ast or value not in legacy):
                raise DriverError("the prototype requires -std=c++17 or newer")
    # These alter the source stream or driver job graph in ways this bootstrap
    # cannot safely reconstruct. Do not silently bypass instrumentation.
    for arg in args:
        if arg in ("-save-temps", "-emit-llvm", "-flto", "--", "-M", "-MM") or arg.startswith(("-x", "-save-temps=", "-fmodules", "-include-pch", "-fmodule-file", "-fprebuilt-module-path", "-MJ", "-fopenmp", "-flto=")):
            raise DriverError(f"unsupported in the bootstrap driver: {arg}")


# Implement the archived JSON-AST backend: ask Clang for semantic source ranges,
# write instrumented copies into temporary storage, then compile those copies. User
# files are never rewritten. This backend deliberately does not cover included headers.
def compile_program(options, args, temporary: Path):
    compiler = options["clang"]
    # Toolchain discovery and preprocessing queries should behave like Clang.
    if not args or any(arg in args for arg in ("--version", "-v", "-dumpmachine", "-print-resource-dir", "-print-search-dirs")) and not source_arguments(args):
        return execute([compiler] + args).returncode
    if "-E" in args:
        return execute([compiler] + args).returncode
    validate(args)
    if not any(a.startswith("-std=") for a in args):
        args = ["-std=c++17"] + args
    sources = source_arguments(args)
    syntax_only = "-fsyntax-only" in args
    no_link = syntax_only or "-c" in args or "-S" in args
    ast_args = analysis_arguments(args, {i for i, _ in sources})
    generated = {}
    reports = []
    destination = Path(options["emit"]).resolve() if options["emit"] else temporary
    destination.mkdir(parents=True, exist_ok=True)
    # Keep each source and its AST/generated file associated by argument index. Large
    # AST dumps go through files rather than captured pipe buffers to limit Python memory use.
    for number, (index, source) in enumerate(sources):
        if not source.is_file():
            raise DriverError(f"source file not found: {source}")
        command = [compiler] + ast_args + ["-fsyntax-only", "-Xclang", "-ast-dump=json", str(source)]
        # Avoid retaining the AST in a pipe buffer or writing user source to a
        # persistent cache; large template-heavy ASTs live only in this tempdir.
        ast_file = temporary / f"ast-{number}.json"
        with ast_file.open("wb") as output:
            status = execute(command, stdout=output).returncode
        if status:
            return status
        try:
            with ast_file.open() as stream:
                ast = json.load(stream)
            transformed, report = Instrumenter(source, source.read_bytes()).run(ast)
        except (ValueError, InstrumentationError, RecursionError) as exc:
            raise DriverError(f"cannot instrument {source}: {exc}") from exc
        target = destination / f"{number}-{source.stem}.strict.cpp"
        if target == source:
            raise DriverError("generated file would overwrite the input")
        target.write_bytes(transformed)
        generated[index] = target
        reports.append(report)
        if not options["quiet"]:
            counts = ", ".join(f"{n} {name}" for name, n in report["counts"].items()) or "no check sites"
            print(f"strictcpp: {source.name}: {counts}", file=sys.stderr)
            print("strictcpp: coverage is main-file only; included headers remain unchecked", file=sys.stderr)
            if report["limitations"]:
                print(f"strictcpp: {len(report['limitations'])} additional coverage limitations (use --sc-report FILE)", file=sys.stderr)
    if options["report"]:
        Path(options["report"]).write_text(json.dumps({"version": __version__, "allow_untracked": options["unknown"],
                                                       "translation_units": reports}, indent=2) + "\n")
    # In AST mode this rejects recorded main-file limitations only. It cannot certify
    # included headers; the native strict source gate is a separate, stronger admission policy.
    if options["require_coverage"]:
        skipped = [(r["source"], note) for r in reports for note in r["limitations"]]
        if skipped:
            for source, note in skipped:
                print(f"{source}:{note['line']}: strictcpp: uncovered: {note['message']}", file=sys.stderr)
            raise DriverError("known main-file coverage gaps rejected by --sc-require-main-coverage")
    compile_args = [str(generated.get(i, arg)) for i, arg in enumerate(args)]
    if "-c" in args and len(sources) == 1 and "-o" not in args:
        compile_args += ["-o", sources[0][1].stem + ".o"]
    if "-S" in args and len(sources) == 1 and "-o" not in args:
        compile_args += ["-o", sources[0][1].stem + ".s"]
    if len(sources) > 1 and no_link:
        raise DriverError("compile each source separately with -c/-S, or link multiple sources in one invocation")
    # Quoted relative includes resolve as they did beside the original source.
    for _, source in sources:
        compile_args = ["-iquote", str(source.parent)] + compile_args
    if sources:
        compile_args += ["-include", str(RUNTIME / "strictcpp.hpp")]
    if options["unknown"]:
        compile_args += ["-DSTRICTCPP_ALLOW_UNTRACKED=1"]
        if not options["quiet"]:
            print("strictcpp: allowing untracked accesses; checks are explicitly weakened", file=sys.stderr)
    if len(sources) > 1 and not no_link:
        # Compile per TU so quoted includes with the same name resolve beside
        # their own original source, even when both sources share a stem.
        for number, (index, source) in enumerate(sources):
            unit_object = temporary / f"unit-{number}.o"
            unit_args = ["-iquote", str(source.parent)] + ast_args
            unit_args += ["-include", str(RUNTIME / "strictcpp.hpp")]
            if options["unknown"]:
                unit_args += ["-DSTRICTCPP_ALLOW_UNTRACKED=1"]
            status = execute([compiler] + unit_args + ["-c", str(generated[index]), "-o", str(unit_object)]).returncode
            if status:
                return status
            generated[index] = unit_object
        compile_args = [str(generated.get(i, arg)) for i, arg in enumerate(args)]
    if not no_link:
        # A separate runtime object prevents user -D/-include options and
        # instrumentation from affecting the checking machinery itself.
        runtime_object = temporary / "strictcpp-runtime.o"
        runtime_flags = ["-std=c++17", "-O2", "-pthread"]
        for i, arg in enumerate(args):
            if arg in ("-arch", "-target", "--target", "-isysroot", "--sysroot") and i + 1 < len(args):
                runtime_flags.extend(args[i:i + 2])
        status = execute([compiler] + runtime_flags + ["-c", str(RUNTIME / "strictcpp.cpp"), "-o", str(runtime_object)]).returncode
        if status:
            return status
        compile_args += [str(runtime_object), "-pthread"]
    return execute([compiler] + compile_args).returncode


# Normalize the public command line once, protect the source output path, and route
# to the selected backend inside one invocation-scoped temporary directory. Cleanup
# also happens on driver errors, so analysis artifacts persist only when requested.
def main(argv=None):
    try:
        args = expand_response_files(list(sys.argv[1:] if argv is None else argv))
        # Accept joined output/dependency spellings without letting -o-prefixed Objective-C
        # or other unrelated options be mistaken for an output filename.
        normalized = []
        for arg in args:
            prefix = next((p for p in ('-MF', '-MT', '-MQ') if arg.startswith(p) and len(arg) > len(p)), None)
            if prefix:
                normalized += [prefix, arg[len(prefix):]]
            elif arg.startswith('-o') and len(arg) > 2 and not arg.startswith(('-objc', '-object', '-openmp')):
                normalized += ['-o', arg[2:]]
            else:
                normalized.append(arg)
        args = normalized
        options, args = parse(args)
        if options is None:
            return 0
        if options["backend"] not in ("native", "ast"):
            raise DriverError("--sc-backend must be native or ast")
        if options["strict"] and options["backend"] != "native":
            raise DriverError("--sc-strict requires the native backend; the AST backend has no strict source gate")
        if '-o' in args and args.index('-o') + 1 < len(args):
            output = Path(args[args.index('-o') + 1]).resolve()
            if any(output == source for _, source in source_arguments(args)):
                raise DriverError("output would overwrite an input source file")
        # The native backend is imported lazily so choosing the AST backend does not require
        # an installed LLVM developer distribution merely to start the launcher.
        with tempfile.TemporaryDirectory(prefix="strictcpp-") as directory:
            if options["backend"] == "native":
                from .native_driver import compile_native
                return compile_native(options, args, Path(directory))
            return compile_program(options, args, Path(directory))
    except (DriverError, OSError) as exc:
        print(f"strictcpp: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
