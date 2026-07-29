"""Repository tooling. Not part of the shipped package — excluded via .dockerignore.

Present so ``tools.check_log_fstrings`` resolves to one module name for both
mypy and the test suite; without it mypy sees the same file as two modules.
"""
