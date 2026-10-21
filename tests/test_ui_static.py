"""Accessibility guardrails for the static front end: markup, colour tokens and the keyboard path."""
import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = (ASSETS / "index.html").read_text(encoding="utf-8")
CSS = (ASSETS / "assets" / "styles.css").read_text(encoding="utf-8")
APP = (ASSETS / "assets" / "js" / "app.js").read_text(encoding="utf-8")
CODING = (ASSETS / "assets" / "js" / "coding.js").read_text(encoding="utf-8")


def _lum(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _token(name):
    return re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", CSS).group(1)


def test_page_basics():
    assert '<html lang="id"' in HTML
    assert 'class="skip"' in HTML and "<main" in HTML
    assert 'name="viewport"' in HTML


def test_colour_tokens_meet_wcag_aa():
    bg, white = _token("bg"), "#ffffff"
    for fg in (_token("ink"), _token("ink-2"), _token("ink-3"), _token("accent")):
        assert _ratio(fg, bg) >= 4.5, fg
        assert _ratio(fg, white) >= 4.5, fg
    assert _ratio(_token("ctl"), white) >= 3 and _ratio(_token("ctl"), bg) >= 3  # 1.4.11 control borders


def test_every_search_box_has_a_label():
    for field in ("qSearch", "kdSearch"):
        assert f'for="{field}"' in HTML and f'id="{field}"' in HTML


def test_lists_and_panels_are_named_and_announced():
    for live in ("queueLede", "kdLede"):
        assert re.search(rf'id="{live}"[^>]*aria-live="polite"|aria-live="polite"[^>]*id="{live}"', HTML)
    assert 'aria-labelledby="qTitle"' in HTML and 'aria-labelledby="kdQTitle"' in HTML
    assert 'role="status"' in HTML or "role','status'" in APP or 'role="status"' in APP


def test_claim_cards_are_keyboard_operable():
    # a real button inside each card, plus arrow-key movement and a visible focus ring
    for src in (APP,):
        assert 'class="cardbtn"' in src and "function listKeys" in src
    assert "listKeys($('rows'))" in APP and "listKeys($('kdRows'))" in CODING
    assert ".cardbtn:focus-visible" in CSS and ":has(.cardbtn:focus-visible)" in CSS


def test_detail_sheet_is_a_dialog_with_focus_handling():
    assert "setAttribute('role','dialog')" in APP and "aria-modal" in APP
    assert "e.key==='Escape'" in APP and "e.key!=='Tab'" in APP
    assert "sheetBack" in APP


def test_save_and_next_flow_is_wired_for_both_pages():
    assert 'value="next"' in APP and 'value="stay"' in APP
    assert "nextAfter(queueList(),c)" in APP and "nextAfter(queueList(),c)" in CODING


def test_no_single_key_shortcuts():
    # WCAG 2.1.4: arrow keys on focused widgets are fine, bare letter shortcuts are not
    assert not re.search(r"key===?'[a-z]'", APP + CODING)


def test_reduced_motion_and_forced_colors_are_handled():
    assert "prefers-reduced-motion" in CSS and "forced-colors" in CSS
