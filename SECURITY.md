# Security and safety reports

Version 0.4.0 is experimental. It does not provide a complete C++ safety
guarantee and is not a production security boundary. There is no supported
stable security release line or promised security-response timeline.

The [safety contract](docs/SAFETY.md) and [audit report](docs/TEST_REPORT.md)
describe current limitations, including executable missed-check witnesses.
Strict mode accepts a deliberately small subset and rejects unsupported constructs;
passing its tests does not prove the compiler or runtime sound.

For ordinary missed checks, compiler crashes and valid-code rejection, open a
GitHub bug report with a minimized source, exact command, selected profile,
toolchain version and actual result. Identify whether strict admission accepted
the source or the broader experimental backend was selected.

If a report contains sensitive details, use GitHub's **Report a vulnerability**
entry in the repository's Security tab when private reporting is enabled. If
it is unavailable, request a private contact channel from a maintainer without
posting the sensitive material. Do not include credentials or private source
code in a public issue. The publishing guide includes enabling private reporting.
