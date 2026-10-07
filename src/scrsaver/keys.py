from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence

try:
    from evdev import ecodes
except ImportError:  # pragma: no cover
    ecodes = None  # type: ignore[assignment]

_SPECIAL = {
    "escape": "KEY_ESC",
    "esc": "KEY_ESC",
    "enter": "KEY_ENTER",
    "return": "KEY_ENTER",
    "space": "KEY_SPACE",
    "tab": "KEY_TAB",
    "backspace": "KEY_BACKSPACE",
    "delete": "KEY_DELETE",
    "insert": "KEY_INSERT",
    "home": "KEY_HOME",
    "end": "KEY_END",
    "pageup": "KEY_PAGEUP",
    "pagedown": "KEY_PAGEDOWN",
    "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "up": "KEY_UP",
    "down": "KEY_DOWN",
}


def normalize_key_name(name: str) -> str:
    text = (name or "Escape").strip()
    return text or "Escape"


def evdev_code_for_name(name: str) -> int | None:
    if ecodes is None:
        return None
    key = normalize_key_name(name)
    special = _SPECIAL.get(key.lower().replace(" ", ""))
    attr = special or f"KEY_{key.upper().replace(' ', '_')}"
    code = getattr(ecodes, attr, None)
    if isinstance(code, int):
        return code
    return getattr(ecodes, "KEY_ESC", 1)


def qt_key_to_name(key: int, text: str = "") -> str:
    if key == Qt.Key.Key_Escape:
        return "Escape"
    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        return "Enter"
    if key == Qt.Key.Key_Space:
        return "Space"
    if key == Qt.Key.Key_Tab:
        return "Tab"
    if key == Qt.Key.Key_Backspace:
        return "Backspace"
    if text and len(text) == 1 and text.isalnum():
        return text.upper()
    label = QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText).strip()
    return label or "Escape"


def sequence_to_name(sequence: QKeySequence) -> str:
    if sequence.isEmpty():
        return "Escape"
    try:
        combination = sequence[0]
        key = combination.key() if hasattr(combination, "key") else int(combination)
    except (IndexError, TypeError, ValueError):
        return "Escape"
    return qt_key_to_name(int(key))


def interactive_notice(exit_key: str) -> str:
    key = normalize_key_name(exit_key)
    return (
        "This demo will stay open so you can click and type in it.\n\n"
        f"Press {key} to exit.\n\n"
        "Change this shortcut in Additional Settings "
        "(right-click the SCR Saver tray icon)."
    )
