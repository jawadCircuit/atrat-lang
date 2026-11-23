# Ecosystem and package-manager plan (later, not now)

**Short answer: yes, a small package ecosystem is the right long-term move, but
it should not be built during 0.8.x.** A package manager freezes APIs. Building
one before Atrat has stable modules, visibility, separate compilation and a
semver policy would turn current compiler shortcuts into ecosystem-wide
compatibility debt.

## Why wait

Today Atrat has:

- module aliases and qualified names now exist, but builds still merge everything into one translation unit; there is no separate compilation, per-field `pub` or package boundary;
- no stable language/spec version or deprecation policy;
- a minimal standard library and no package boundary;
- one compiler implementation and no binary/ABI contract.

Those are exactly the foundations a package manager needs. The compiler work
listed in `docs/READINESS.md` comes first.

## Prerequisites, in order

1. **Module boundaries.** Namespacing, explicit `pub`/private visibility,
   separate compilation, and a clear rule for what a package exports.
2. **Versioning.** Semver policy, deprecation warnings, and a stable manifest
   schema before a registry exists.
3. **Reproducible builds.** Deterministic compiler output, source hashes, and a
   lockfile that pins transitive dependency versions.
4. **A small stable stdlib.** Collections, filesystem, formatting, testing and a
   process/CLI baseline. Libraries cannot be built on APIs that change weekly.
5. **A security model.** Package identity, checksums, a review/abuse process,
   and no arbitrary install-time scripts.

## Small MVP (after the prerequisites)

Keep the first version deliberately boring:

- `atr.toml` manifest: name, version, edition, dependencies, targets.
- `atrc new`, `atrc build`, `atrc test`, `atrc add`.
- **Local path dependencies first**, then Git dependencies, then an index.
- A lockfile with exact versions and SHA-256 content hashes.
- A static registry index (a Git repository or signed JSON), not a hosted
  service with accounts and binary uploads.
- Vendored source directories and offline builds.
- No post-install scripts, no native C dependency resolver, no dynamic plugins.

Only after that is stable should there be a hosted registry, namespaces,
ownership transfer, yanking policy, and a binary cache.

## Library ecosystem priorities

Build a small set of libraries that exercise the language rather than trying to
match Python or Rust:

1. `std` cleanup and documentation.
2. JSON encode/decode.
3. CLI argument parsing and terminal output.
4. HTTP client and a minimal server.
5. Testing/assertion helpers.
6. Serialization and configuration.
7. Collections beyond `List` (maps, sets, iterators).

The goal is a coherent small ecosystem, not a clone of crates.io or PyPI.

## What not to do

- Do not publish a registry before module visibility and separate compilation.
- Do not promise semver before the language has a 1.0 specification.
- Do not make install-time scripts part of the package format.
- Do not build a package manager as a way to avoid fixing the compiler.
- Do not add a second package format for "just this one tool".

## Relationship to 1.0

A package manager is a **1.0 gate**, not a 0.8.x feature. The realistic sequence
is: 0.8.x hardening → module/visibility and lifetime work → local package MVP →
registry beta → 1.0 with a stability and security process. See the estimate in
`docs/READINESS.md`; this is a 2028+ project on the current full-time estimate,
not something to rush into now.
