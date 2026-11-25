# Test fixtures

`golden_test_key.pem` is a throwaway 2048-bit RSA key generated only to pin
the request-signing format: the tests sign a known canonical string with it
and compare against recorded vectors, which cannot work with a key that is
different on every run.

It has never been enrolled with Duo and grants nothing. Do not reuse it for
anything, and do not copy it into a vault.

The repository ignores `*.pem`; `.gitignore` makes a single exception for
this file.
