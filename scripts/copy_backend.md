# Copying the Python backend source tree

From the Hermes checkout root, generate a separate source copy:

```bash
python3 scripts/copy_backend.py
```

The output is `hermes-backend/`. To refresh that output after changes, run:

```bash
python3 scripts/copy_backend.py --replace
```

`--replace` only replaces a directory bearing this generator's ownership marker for the same source checkout. The generator stages the new tree beside the destination before publication. Existing unmarked destinations, symlink destinations, and outputs from another source root are refused. The original checkout is not moved or rewritten.

The copy retains the Python package roots declared by `pyproject.toml`, top-level Python modules, runtime resource directories (`skills/`, `optional-skills/`, `locales/`, plugin data, and assets), root launcher/package metadata, and nested package resources. It omits frontend trees, tests, local environments, VCS metadata, caches, and `.env` files. It is a source bundle, not a prebuilt isolated runtime: dependency/environment setup and independent CLI, gateway, HTTP, and JSON-RPC smoke verification are separate steps.
