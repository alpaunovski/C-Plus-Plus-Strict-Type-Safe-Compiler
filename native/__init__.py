# This package exposes Python build/toolchain helpers; the actual checking pass and
# runtime live in the C++ files alongside it. Keeping the package lightweight avoids
# loading or building LLVM components merely by importing its namespace.
"""Native LLVM instrumentation backend."""
