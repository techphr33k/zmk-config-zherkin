# Zherkin ZMK configuration

ZMK firmware configuration for the Zherkin keyboard using a nice!nano v2.
The build target is defined in [build.yaml](build.yaml), and the keymap lives
in [boards/shields/zherkin/zherkin.keymap](boards/shields/zherkin/zherkin.keymap).

Before submitting a keymap change, run the quick syntax check from this
repository's root:

```sh
python zmk_check.py boards/shields/zherkin/zherkin.keymap --syntax-only
```

The [keymap checker guide](docs/zmk-check.md) covers setup, full static checks,
diagnostics, and tests. The checker catches common mistakes locally; the
GitHub Actions firmware build verifies compilation.

Submit changes on a topic branch and open a pull request against `main`.
Explain the behavior change and include the checks you ran. Wait for the
firmware build and review before merging.
