"""Terminal-safe display of untrusted text (remote or imported diagnostics)."""
import re

_CONTROL = re.compile('[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def terminal_safe(text):
    """Show C0 (except tab and newline), DEL and C1 control characters as visible \\xNN escapes; other text, UTF-8 included, is kept."""
    return _CONTROL.sub(lambda m: f'\\x{ord(m.group()):02x}', str(text))
