.PHONY: check test test-native test-ast test-standards test-boundaries verify-runtime bootstrap examples demo strict-demo clean

# The complete suite includes compatibility regressions, strict admission tests,
# driver integrity checks and expected failures that expose known safety limits.

check:
# Pure Python/Git publication checks need no LLVM download or native host.
	python3 scripts/check_repository.py

test:
	python3 -m unittest discover -s tests -v

test-native:
# Keep both native runtime semantics and the independent source admission policy
# visible when running the native-only subset; the archived AST suite is separate.
	python3 -m unittest discover -s tests -p test_native.py -v
	python3 -m unittest discover -s tests -p test_semantics.py -v
	python3 -m unittest discover -s tests -p test_strict.py -v

test-ast:
	python3 -m unittest discover -s tests -p test_compiler.py -v

test-standards:
# This slower differential audit preserves known and newly observed failures.
# A nonzero exit is expected while the compiler has unresolved safety gaps.
	python3 tests/standards_matrix.py

test-boundaries:
# Run serially after test-standards: both suites use the native build cache.
# Foreign calls, varargs, assembly and nonlocal jumps have separate evidence.
	python3 tests/standards_supplement.py

verify-runtime:
# ASan/UBSan instrument the trusted checker implementation, not the compiler
# plugins. This supplements end-to-end source tests with runtime implementation QA.
	python3 native/verify_runtime.py

bootstrap:
# Download/verify the pinned developer distribution inside .strictcpp/, then
# compile matching plugins and the runtime. The system compiler is not replaced.
	python3 native/bootstrap.py

examples: build/valid build/type_confusion build/heap_array build/vector_lifetime build/replacement_allocator build/union_members build/union_lifetime build/character_alias

build:
	mkdir -p build

build/%: examples/%.cpp bin/strict++ strictcpp/driver.py strictcpp/native_driver.py strictcpp/policy.py native/pass.cpp native/frontend.cpp native/strict_profile.h native/runtime.cpp native/abi.h native/build.py | build
# Compiler implementation dependencies force examples to refresh after checker
# changes. Native build.py separately caches the actual plugin/runtime artifacts.
	./bin/strict++ -O2 $< -o $@

build/strict: examples/strict.cpp bin/strict++ strictcpp/driver.py strictcpp/native_driver.py strictcpp/policy.py native/pass.cpp native/frontend.cpp native/strict_profile.h native/runtime.cpp native/abi.h native/build.py | build
# The explicit rule takes precedence over build/%: this example must pass the
# strict source/driver allowlists, and its JSON report records that admission mode.
	./bin/strict++ --sc-strict -O2 --sc-report build/strict-coverage.json $< -o $@

strict-demo: build/strict
	./build/strict

demo: examples
# Negative examples intentionally terminate with 86. Capture their status so Make
# succeeds only when each expected rejection actually occurs; arbitrary failures
# and accidental successful executions both make this target fail.
	./build/valid
	./build/heap_array
	./build/replacement_allocator
	./build/union_members
	@./build/type_confusion; result=$$?; test $$result -eq 86
	@./build/vector_lifetime; result=$$?; test $$result -eq 86
	@./build/union_lifetime; result=$$?; test $$result -eq 86
	@./build/character_alias; result=$$?; test $$result -eq 86

clean:
# Remove generated build products only. Keep the verified LLVM download/install
# under .strictcpp/ so a routine rebuild does not require another large download.
	rm -rf build
