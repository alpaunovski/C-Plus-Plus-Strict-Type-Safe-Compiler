# Driver admission must depend on complete, unambiguous evidence from each stage.
# These tests corrupt successful subprocess reports and probe alternate driver paths;
# a successful tool exit alone is never sufficient to authorize unchecked output.
"""A successful compiler subprocess must not silently produce unchecked output."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from strictcpp import driver
from strictcpp.native_driver import compile_native


class DriverIntegrityTests(unittest.TestCase):
    # Delete/duplicate each real stage summary after a successful tool run. The driver must
    # raise a policy error and never link an executable merely because the subprocess returned zero.
    def test_missing_or_ambiguous_instrumentation_reports_stop_build(self):
        for marker in ('strictcpp-front:', 'strictcpp-native:', 'strictcpp-native-foreign:'):
            for corruption in ('missing', 'duplicate'):
                with self.subTest(marker=marker, corruption=corruption), tempfile.TemporaryDirectory() as folder:
                    folder = Path(folder)
                    source = folder / 'main.cpp'
                    source.write_text('int main(){int n=7;return *&n!=7;}')
                    output = folder / 'program'
                    options, args = driver.parse(['--sc-quiet', str(source), '-o', str(output)])
                    real_execute = driver.execute

                    # Run the actual compiler/pass first, changing only its captured evidence. This isolates
                    # report-integrity behavior from unrelated fake-tool failures or invalid generated IR.
                    def corrupt_report(command, **kwargs):
                        result = real_execute(command, **kwargs)
                        if isinstance(result.stderr, str):
                            lines = result.stderr.splitlines(keepends=True)
                            selected = [line for line in lines if line.startswith(marker)]
                            if corruption == 'missing':
                                result.stderr = ''.join(line for line in lines if not line.startswith(marker))
                            else:
                                result.stderr += ''.join(selected)
                        return result

                    with patch('strictcpp.driver.execute', side_effect=corrupt_report):
                        with self.assertRaisesRegex(driver.DriverError, 'missing or ambiguous'):
                            compile_native(options, args, folder / 'temporary')
                    self.assertFalse(output.exists())

    # A user pragma may resemble report text. Only exact anchored stage summaries may determine
    # coverage; an embedded diagnostic substring cannot replace the real deferred count.
    def test_source_diagnostic_cannot_hide_deferred_sites(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / 'main.cpp'
            source.write_text('#pragma message("0 placement hooks, 0 semantic sites deferred")\n'
                              'int main(){int n=7;int* p=&n;return *p!=7;}')
            result = subprocess.run([str(driver.ROOT / 'bin/strict++'), '--sc-quiet', '--sc-require-coverage',
                                     str(source), '-o', str(folder / 'program')], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('deferred frontend sites', result.stderr)

    # Reject joined language selection, raw IR and linker-forwarded inputs that could make
    # Clang compile/link a file outside the wrapper's instrumented source pipeline.
    def test_alternate_inputs_cannot_bypass_instrumentation(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / 'main.cpp'
            source.write_text('int main(){}')
            unrecognized = folder / 'main.input'
            unrecognized.write_text('int main(){}')
            cases = [
                ['-xc++', str(unrecognized)],
                ['--sc-require-coverage', str(unrecognized)],
                ['--sc-require-coverage', str(source), '-Wl,hidden.o'],
                ['--sc-require-coverage', str(source), '-Xlinker', 'hidden.o'],
                ['--sc-require-coverage', str(source), 'unchecked.bc'],
            ]
            output = folder / 'program'
            for args in cases:
                with self.subTest(args=args):
                    result = subprocess.run([str(driver.ROOT / 'bin/strict++'), '--sc-quiet', *args,
                                             '-o', str(output)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse(output.exists())


if __name__ == '__main__': unittest.main()
