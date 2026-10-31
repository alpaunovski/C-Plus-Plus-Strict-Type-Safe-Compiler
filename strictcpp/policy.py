"""Closed driver policy for the strict-v1 source subset."""
from pathlib import Path
import platform
import re
import sys

# The profile name is part of the compiler/driver acknowledgment and JSON evidence.
# Changing admission rules should be reviewed as a profile-contract change, not an
# ordinary compatibility expansion.
PROFILE = 'strict-v1'


# Admit only the tested source-to-executable job configuration. Returning here for
# experimental mode is deliberate: the broader backend retains its documented limits,
# while --sc-strict cannot silently fall back to it.
def validate_strict(options, args):
    from .driver import DriverError, source_arguments
    if not options.get('strict'):
        return
    # Target and environment assumptions are kept narrow until another ABI is validated.
    # LLVM itself accepts more platforms; that does not make this runtime/profile portable.
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise DriverError('strict-v1 is currently validated only for macOS arm64')
    if options['unknown']:
        raise DriverError('strict mode cannot permit untracked storage')
    # Command-line macro definitions are copied into Clang predefines. Embedded newlines
    # could inject directives into that otherwise trusted buffer, so reject them before parsing.
    if any(any(character in arg for character in ('\n', '\r', '\0')) for arg in args):
        raise DriverError('strict-v1 rejects control characters in compiler arguments')
    sources = source_arguments(args)
    if len(sources) != 1:
        raise DriverError('strict-v1 requires exactly one source translation unit')
    # Enumerate accepted options rather than rejecting a few dangerous spellings.
    # In particular, compiler/pass forwarding and raw assembler/linker directives are
    # outside this profile even if they would be valid normal Clang options.
    operands = {'-o', '-I', '-D', '-U'}
    flags = {'-O0', '-O1', '-O2', '-O3', '-Os', '-Oz', '-g', '-g0',
             '-std=c++17', '-std=gnu++17', '-fno-rtti', '-fno-exceptions'}
    # Source classification uses original argument indexes. Operand values such as an
    # include path or macro definition must never be reinterpreted as additional sources.
    source_indexes = {index for index, _ in sources}
    outputs = []
    i = 0
    while i < len(args):
        arg = args[i]
        if i in source_indexes:
            i += 1
            continue
        if arg in operands:
            if i + 1 == len(args):
                raise DriverError(f'missing value after {arg}')
            if arg == '-o': outputs.append(Path(args[i + 1]).resolve())
            i += 2
            continue
        if arg in flags or arg in ('-Wall', '-Wextra', '-Wpedantic', '-Werror') or re.fullmatch(r'-W(?:no-)?(?:error=)?[a-z][a-z0-9-]*', arg) or any(
                arg.startswith(prefix) and len(arg) > len(prefix) for prefix in ('-I', '-D', '-U')):
            i += 1
            continue
        raise DriverError(f'strict-v1 rejects compiler option or input: {arg}')
    # A single output path makes the artifact relationship unambiguous. Reports are also
    # kept distinct from source/output paths so documentation requests cannot overwrite input.
    if len(outputs) > 1:
        raise DriverError('strict-v1 requires at most one output path')
    source = sources[0][1]
    output = outputs[0] if outputs else Path('a.out').resolve()
    report = Path(options['report']).resolve() if options['report'] else None
    if output == source or report == source or report == output:
        raise DriverError('strict-v1 output/report paths must be distinct from the source and each other')
