"""Run unchanged bundled scripts with Python 3.10 zip(strict) semantics."""
import builtins
import itertools
import runpy
import sys
from pathlib import Path

_original_zip = builtins.zip


def _strict_rows(iterables):
    sentinel = object()
    for row in itertools.zip_longest(*iterables, fillvalue=sentinel):
        if any(value is sentinel for value in row):
            raise ValueError("zip() arguments have different lengths")
        yield row


def compat_zip(*iterables, strict=False):
    if strict:
        return _strict_rows(iterables)
    return _original_zip(*iterables)


def main():
    builtins.zip = compat_zip
    script = str(Path(sys.argv[1]).resolve())
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(Path(script).parent))
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
