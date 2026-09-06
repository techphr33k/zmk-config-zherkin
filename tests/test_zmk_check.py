"""Run from the repo root: python -B -m unittest discover -s tests -v

Set ZMK_TEST_SRC and ZMK_TEST_CPP to also exercise a real C preprocessor.
Zephyr's include directory must be adjacent to the ZMK checkout.
"""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zmk_check as checker


class CheckerTests(unittest.TestCase):
    def structure(self, text):
        report = checker.Report()
        checker.check_structure(Path("test.keymap"), text, report)
        return report

    def test_missing_binding_close(self):
        report = self.structure('/ { keymap { layer {\n bindings = <&kp A\n }; }; };')
        self.assertEqual([(d.code, d.line) for d in report.diags], [("unclosed-cells", 3)])

    def test_missing_semicolon(self):
        report = self.structure('/ { layer { bindings = <&kp A>\n }; };')
        self.assertEqual(report.diags[0].code, "missing-semicolon")

    def test_comments_strings_and_directives(self):
        report = self.structure('#include <behaviors.dtsi>\n#define SHIFT (1 << 4)\n'
                                '/ { label = "<{}>"; // >\n value = <(1 << 4)>; /* < */ };')
        self.assertEqual(report.errors, 0)

    def test_binding_locations_preserve_whitespace(self):
        value = '\n   <\n &kp (1 << (2 + 3))\n &none >'
        invocations = checker.parse_bindings(value, 50)
        self.assertEqual(invocations[0].offset, 50 + value.index('&kp'))
        self.assertEqual(invocations[0].cells, ['(1 << (2 + 3))'])
        self.assertEqual(invocations[1].cells, [])

    def test_behavior_cell_count_is_not_inherited_from_child(self):
        behaviors, _ = checker.collect_behaviors(
            'outer: node { inner: key { #binding-cells = <1>; }; };')
        self.assertEqual(behaviors, {'inner': 1})

    def test_integer_expressions_are_bounded(self):
        self.assertEqual(checker.eval_cell('(0x70000 | 40)'), 458792)
        self.assertEqual(checker.eval_cell('-5 / 2'), -2)
        self.assertEqual(checker.eval_cell('-5 % 2'), -1)
        for expression in ('2 ** 1000000000', '1 << 1000000000', 'f()', 'x', '1 / 0'):
            self.assertIsNone(checker.eval_cell(expression))

    def test_line_map_decodes_windows_paths(self):
        line_map = checker.LineMap('# 40 "C:\\\\src\\\\board.keymap"\n&kp BAD\n', 'default')
        self.assertEqual(line_map.lookup(2), ('C:\\src\\board.keymap', 40))

    def test_invalid_explicit_checkout_does_not_fall_back(self):
        with self.assertRaises(checker.SetupError):
            checker.find_zmk('nonexistent-zmk-test-checkout', False)

    def test_structure_runs_before_source_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'broken.keymap'
            path.write_text('/ { layer { bindings = <&kp A\n }; };')
            with patch('sys.argv', ['zmk_check.py', str(path)]), \
                    patch.object(checker, 'find_zmk') as find, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checker.main(), 1)
                find.assert_not_called()

    def test_syntax_only_does_not_claim_to_resolve_conditionals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'conditional.keymap'
            path.write_text('#if 0\n{\n#endif\n')
            with patch('sys.argv', ['zmk_check.py', '--syntax-only', str(path)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checker.main(), 1)


@unittest.skipUnless(os.environ.get('ZMK_TEST_SRC') and os.environ.get('ZMK_TEST_CPP'),
                     'set ZMK_TEST_SRC and ZMK_TEST_CPP for real compiler tests')
class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.keymap'
        self.zmk = Path(os.environ['ZMK_TEST_SRC'])
        self.cpp = os.environ['ZMK_TEST_CPP']

    def check(self, binding='&kp ENTER', extra='', before='', key_count=1):
        self.path.write_text('#include <behaviors.dtsi>\n'
                             '#include <dt-bindings/zmk/keys.h>\n'
                             '#include <dt-bindings/zmk/bt.h>\n' + before +
                             '/ {\n keymap {\n compatible = "zmk,keymap";\n'
                             ' base {\n bindings = <\n' + binding + '\n>;\n };\n };\n' +
                             extra + '\n};\n', encoding='utf-8')
        report = checker.Report()
        checker.check_keymap(self.path, self.zmk, report, key_count, self.cpp)
        return report

    def test_valid_keycode_and_modifiers(self):
        self.assertEqual(self.check('&kp LC(LS(A))').errors, 0)

    def test_original_undefined_enter_keycode_location(self):
        report = self.check('&kp ENT')
        diag = next(d for d in report.diags if d.code == 'undefined-keycode')
        self.assertEqual(diag.line, 9)
        self.assertEqual(Path(diag.file).resolve(), self.path.resolve())

    def test_wrong_cell_count(self):
        self.assertIn('cell-count', [d.code for d in self.check('&lt 0').diags])

    def test_unknown_behavior(self):
        self.assertIn('unknown-behavior', [d.code for d in self.check('&typo A').diags])

    def test_bluetooth_macros_supply_default_parameter(self):
        self.assertEqual(self.check('&bt BT_CLR_ALL &bt BT_SEL 0', key_count=2).errors, 0)

    def test_bad_layer_indices(self):
        for binding in ('&mo 1', '&mo (-1)'):
            self.assertIn('bad-layer', [d.code for d in self.check(binding).diags])

    def test_missing_include_is_fatal(self):
        report = self.check(before='#include "misspelled-header.h"\n')
        self.assertIn('preprocess', [d.code for d in report.diags])
        self.assertIn('misspelled-header.h', report.diags[0].msg)

    def test_inactive_invalid_branch_is_ignored(self):
        self.assertEqual(self.check(before='#if 0\n{ < broken\n#endif\n').errors, 0)

    def test_structure_error_introduced_by_macro(self):
        report = self.check('BAD', before='#define BAD &kp A > >\n')
        self.assertTrue(any(d.code == 'stray-angle' for d in report.diags))

    def test_combo_range(self):
        extra = 'combos { compatible = "zmk,combos"; c { key-positions = <0 1>; bindings = <&kp A>; }; };'
        self.assertIn('combo-range', [d.code for d in self.check(extra=extra).diags])

    def test_layer_size_from_overlay(self):
        self.path.with_suffix('.overlay').write_text(
            '/ { transform { compatible = "zmk,matrix-transform"; map = <RC(0,0) RC(0,1)>; }; };')
        report = self.check(key_count=None)
        self.assertIn('layer-size', [d.code for d in report.diags])

    def test_numeric_cell_without_behavior(self):
        self.assertIn('orphan-cell', [d.code for d in self.check('A').diags])


if __name__ == '__main__':
    unittest.main()
