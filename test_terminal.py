"""terminal_safe: untrusted text is shown, never interpreted by the terminal."""
import unittest

from tokenatlas.terminal import terminal_safe


class TerminalSafeTest(unittest.TestCase):
    def test_escapes_c0_del_and_c1(self):
        out = terminal_safe('a\x1b[2Jb\x1b]52;c;ZXZpbA==\x07c\rd\x00e\x7ff\x85g\x9bh')
        self.assertEqual(out, 'a\\x1b[2Jb\\x1b]52;c;ZXZpbA==\\x07c\\x0dd\\x00e\\x7ff\\x85g\\x9bh')
        self.assertFalse([c for c in out if ord(c) < 32 or 0x7f <= ord(c) <= 0x9f])

    def test_keeps_tab_newline_and_readable_text(self):
        text = 'ssh: Permission denied (publickey).\n\tcafe \u00e9 \u2603 \u4e2d'
        self.assertEqual(terminal_safe(text), text)

    def test_accepts_exceptions(self):
        self.assertEqual(terminal_safe(ValueError('bad\x1b[0m')), 'bad\\x1b[0m')


if __name__ == '__main__':
    unittest.main()
