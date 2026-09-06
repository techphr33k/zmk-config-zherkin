# Checking a keymap before building

`zmk_check.py` catches common keymap errors locally, including missing bindings
delimiters, unknown keycodes, incorrect behavior parameters, and layer sizes.
It is a static checker, not a firmware compiler or a complete devicetree
validator. A passing result still needs a ZMK firmware build.

Run the commands below from the root of this repository. The checker itself
uses only the Python standard library and requires Python 3.10 or later.

## Quick syntax check

```sh
python zmk_check.py boards/shields/zherkin/zherkin.keymap --syntax-only
```

On systems where Python is named `python3`, use that command instead. This
check needs no ZMK checkout or C compiler. It checks delimiter balance and
semicolons after cell arrays, but does not expand includes or validate
bindings. Files with `#if`/`#ifdef` directives require preprocessing and report
an error in syntax-only mode.

For example, an unclosed layer bindings block produces:

```text
zherkin.keymap:83: error: [unclosed-cells] '}' closes a node while a '<' value block opened at line 79 is still open - you are probably missing a '>;'
```

Close the array with `>;` before the layer's closing `};`:

```dts
R3 {
    bindings = <
        /* key bindings */
    >;
};
```

## Check expanded bindings

Install GCC, Clang, or Zig and provide ZMK and matching Zephyr headers. An
existing ZMK development workspace normally has this layout:

```text
workspace/
  zmk-config-zherkin/  # this repository
  zmk/
    app/dts/
    app/include/
  zephyr/
    include/
```

With that layout, run:

```sh
python zmk_check.py boards/shields/zherkin/zherkin.keymap --zmk ../zmk
```

The checker searches PATH for `cpp`, `clang-cpp`, `clang`, `gcc`, or `zig`.
Select a compiler explicitly with `--cpp`. For example, in PowerShell:

```powershell
python .\zmk_check.py .\boards\shields\zherkin\zherkin.keymap `
  --zmk ..\zmk `
  --cpp 'C:\Program Files\LLVM\bin\clang.exe'
```

For Zig, pass the path to `zig` or `zig.exe`; the checker adds `cc` itself.
Shell aliases, including PowerShell's `cpp` alias, are not executables and
are not used.

### Download just the headers

A full Zephyr SDK is not needed for static checks. These commands create
sparse source checkouts next to the configuration repository:

```sh
git clone --depth 1 --filter=blob:none --sparse --branch main https://github.com/zmkfirmware/zmk.git ../zmk
git -C ../zmk sparse-checkout set app/dts app/include
```

Use the ZMK remote and revision declared in `config/west.yml` if they differ
from this example. Then read `../zmk/app/west.yml` and find the `zephyr`
project's remote and revision. At the time this guide was added, they were
`zmkfirmware` and `v4.1.0+zmk-fixes`:

```sh
git clone --depth 1 --filter=blob:none --sparse --branch v4.1.0+zmk-fixes https://github.com/zmkfirmware/zephyr.git ../zephyr
git -C ../zephyr sparse-checkout set include
python zmk_check.py boards/shields/zherkin/zherkin.keymap --zmk ../zmk
```

Match these revisions to the firmware build when updating dependencies.
`--fetch` can instead download ZMK's default branch into
`~/.cache/zmk-check/zmk`; it downloads **only ZMK**, does not update existing
checkouts, and does not resolve West manifests or download Zephyr.

### Additional options

| Option | Purpose |
| --- | --- |
| `--zmk PATH` | Select a ZMK source checkout; `ZMK_SRC` is also supported. |
| `--cpp PATH` | Select a compiler executable; `ZMK_CPP` is also supported. |
| `-I PATH` | Add an include directory; repeat for Zephyr or custom module headers. |
| `-D NAME=VALUE` | Add a preprocessor definition; repeat for required configuration macros. |
| `--keys 30` | Override automatic key-count detection. |
| `--no-color` | Disable colored diagnostic labels. |

The sibling `zephyr/include` directory is added automatically. If Zephyr is
elsewhere, pass `-I /path/to/zephyr/include`. Missing headers are errors;
the checker does not substitute empty headers.

## Reading the results

Diagnostics include a source file, line number, and a code identifying the
problem. For example, `&kp ENT` produces `undefined-keycode`: ZMK defines
`ENTER`, `RETURN`, and `RET`, but not `ENT` in the version used for this fix.
Replace it with `&kp ENTER`.

The checker detects 30 keys from the Zherkin matrix transform and checks each
layer's binding count. It also checks behavior parameter counts, direct
layer indices, and combo positions against the detected layout.

The current keymap has one advisory: `QWERTY` is both a node name and a C macro,
so the node name expands to `0`. The `name-macro-collision` warning does not
fail the check.

| Exit code | Meaning |
| --- | --- |
| `0` | No errors detected in the selected checks; warnings are allowed. |
| `1` | Keymap errors, preprocessing errors, or no keymap files found. |
| `2` | Setup or usage failure, such as a missing compiler or invalid `--zmk`. |

Raw syntax checks run before source discovery, so straightforward delimiter
errors are reported even if the headers or compiler are not installed.

The checker does not merge the board's complete devicetree, resolve all
overlays or custom module behaviors, validate every behavior property, or
run Kconfig, the firmware compiler, or linker. It does not infer configuration
macros from `.conf` files. Key-count detection is a heuristic; use `--keys` for
layouts whose transforms are included elsewhere or have several alternatives.

The repository's **Build ZMK firmware** workflow remains the compilation
check. For local firmware builds, follow ZMK's
[building and flashing guide](https://zmk.dev/docs/development/local-toolchain/build-flash).
The [build troubleshooting guide](https://zmk.dev/docs/troubleshooting/building-issues)
explains the corresponding compiler errors.

## Testing changes to the checker

Run the unit tests:

```sh
python -B -m unittest discover -s tests -v
```

Integration tests are skipped unless both `ZMK_TEST_SRC` and `ZMK_TEST_CPP`
are set. To include them with the workspace above, use Bash:

```sh
ZMK_TEST_SRC=../zmk ZMK_TEST_CPP=gcc python -B -m unittest discover -s tests -v
```

Or PowerShell:

```powershell
$env:ZMK_TEST_SRC = (Resolve-Path ..\zmk).Path
$env:ZMK_TEST_CPP = 'C:\Program Files\LLVM\bin\clang.exe'
python -B -m unittest discover -s tests -v
```

The tests cover the two original keymap errors, diagnostic locations,
behavior parameters, missing includes, conditionals, layer sizes, and combo
positions. CI runs unit and compiler integration tests against pinned ZMK
and Zephyr header fixtures on Python 3.10 and 3.14. Those fixture pins are
for checker regression tests; the firmware workflow uses `config/west.yml`.
