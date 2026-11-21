# Releasing Atrat

This is the release checklist for the alpha line. Atrat is **not
production-ready**; every public release must say so.

## 1. Decide the version

- Compiler version lives in `atrat/version.py`.
- Package version lives in `pyproject.toml`.
- Both must match, and `CHANGELOG.md` needs an entry.
- Alpha tags use `v0.8.2-alpha`; package versions use `0.8.2`.

Check:

```bash
python atrc --version
python tests/run_tests.py
```

## 2. Replace placeholders

The repository is `jawadCircuit/atrat-lang`. If you fork or transfer it, update
the URLs in `README.md` and `pyproject.toml`.

## 3. Build release artifacts

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m pip install --force-reinstall dist/atrat_lang-0.8.2-py3-none-any.whl
atrc --version
```

Do not commit `build/`, `dist/` or `*.egg-info/`.

## 4. Create the GitHub repository

Using the website: create an empty repository named `atrat-lang`, public, without a
README or license (this repository already has both).

Or with GitHub CLI:

```bash
gh auth login
gh repo create atrat-lang --public --source=. --remote=origin --push
```

## 5. Commit and tag

```bash
git add .
git commit -m "Atrat 0.8.2-alpha"
git branch -M main
git tag -a v0.8.2-alpha -m "Atrat 0.8.2-alpha"
git push -u origin main
git push origin v0.8.2-alpha
```

## 6. Create the GitHub Release

```bash
gh release create v0.8.2-alpha \
  dist/* \
  --title "Atrat 0.8.2-alpha" \
  --prerelease \
  --notes-file release-notes.md
```

Or use **Releases → Draft a new release**, select the tag, attach the wheel and
sdist, and tick **This is a pre-release**.

Use wording like this, and keep the limitations in:

> Atrat 0.8.2-alpha is a working compiler for experiments and prototypes.
> It is not production-ready: lifetimes are conservative, non-Copy closures
> cannot be captured, modules have no visibility enforcement, and there is no
> threading/package ecosystem yet. Safe-code memory safety is tested but has
> not been audited. Expect breaking changes.

## 7. Optional: publish to PyPI

PyPI package names are global. Check that `atrat-lang` is still available first.
Then:

```bash
python -m twine upload dist/*
```

Only publish after the GitHub release has passed a clean install test.

## 8. Verify the first public build

- GitHub Actions is green on Ubuntu and Windows.
- The Windows job found a C compiler (`gcc --version`).
- `python atrc run examples/demo.atr` works on a clean checkout.
- The wheel installs in a fresh virtual environment.
- The release is marked **pre-release**, not “Latest”.

If the Windows compiler step fails, fix CI before announcing the release.
