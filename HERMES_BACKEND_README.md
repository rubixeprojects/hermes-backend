# Hermes Python backend source copy

This folder is a separate copy of the Python backend source and its runtime assets. TypeScript
frontends, tests, local credentials, VCS data, environments, and caches are not included.

The copy preserves the original Python package layout. It is not a prebuilt isolated runtime;
set up Python and the project dependencies here before launching Hermes. Use this directory as
the working tree for backend changes.

To regenerate from the original checkout, run `python3 scripts/copy_backend.py --replace`
from that checkout's root. Only an output created by that generator from the same source root
can be replaced.
