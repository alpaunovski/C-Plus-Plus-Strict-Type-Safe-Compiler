---
name: Compiler bug
about: Report a missed safety check, valid-code rejection, crash or driver defect.
title: ''
labels: ''
assignees: ''
---

### Behavior

State the expected result and the actual result. Identify a missed check,
valid-code rejection, crash, or unexpected admission decision.

### Minimal unchanged C++ source

```cpp
// Small complete reproduction, including headers when needed.
```

### Exact command and diagnostic

```sh
./bin/strict++ --version
# Include the actual compilation command and complete relevant diagnostic.
```

### Environment

- macOS version and architecture:
- LLVM/Clang version:
- Native, coverage-rejecting, strict, or archived AST mode:
- Standard and optimization flags:
- Relevant coverage report or emitted IR, if available:

### Existing evidence

Mention related fixtures or findings in `docs/TEST_REPORT.md`, if any. Follow
`SECURITY.md` for sensitive reports; omit credentials and private source.
