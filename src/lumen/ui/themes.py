"""Theme registration for Lumen's TUI.

We ship two themes calibrated toward the opencode aesthetic — a soft dark
theme as the default and a neutral light theme as the alternative. Both are
plain ``textual.theme.Theme`` instances, so Textual derives text/muted/boost
shades automatically from the background and primary colors.

Why not adopt posting's high-saturation ``galaxy`` palette? Prolonged sessions
on saturated magenta/cyan backgrounds cause more visual fatigue than low-
saturation blue-gray, and the opencode look the user asked for is deliberately
muted. We keep the ``register_theme`` pattern from posting (see
``posting/app.py:1417``) but with our own palette.
"""

from __future__ import annotations

from textual.app import App
from textual.theme import Theme

#: The theme name set on app start (``app.theme = DEFAULT_THEME``).
DEFAULT_THEME = "lumen-dark"

#: Soft dark theme — GitHub-dark-inspired palette with calibrated contrast.
#: Values chosen for >= 7:1 contrast (WCAG AAA) between background and body
#: text, with primary/accent providing clear call-to-action affordance.
#: The palette follows pi/tui's "conservative defaults" philosophy: low-
#: saturation backgrounds, high-luminance text, one accent color for
#: interaction state.
LUMEN_DARK = Theme(
    name=DEFAULT_THEME,
    primary="#58A6FF",  # GitHub blue — high recognition, still soft
    secondary="#56D4DD",  # teal — distinguishes progress/commentary from accent
    background="#0D1117",  # GitHub dark base — pure dark with blue undertone
    surface="#161B22",  # card / bubble background (lifts +2 luminance from bg)
    panel="#21262D",  # nested surface (dropdowns, popovers — lifts from surface)
    foreground="#E6EDF3",  # near-white body text (15.5:1 on background = AAA)
    warning="#D29922",
    error="#F85149",
    success="#3FB950",
    accent="#79C0FF",  # light sky-blue for interaction highlights
    dark=True,
    variables={
        # Match the prompt editor's cursor to the primary so the focus state
        # reads as part of the theme rather than a Textual default.
        "input-cursor-background": "#58A6FF",
        "input-cursor-foreground": "#0D1117",
        "input-selection-background": "#58A6FF 30%",
        "footer-background": "transparent",
        # Scrollbars tinted with primary so the 1-cell chrome reads as themed.
        "scrollbar-color": "#58A6FF 30%",
        "scrollbar-color-hover": "#58A6FF 70%",
        "scrollbar-color-active": "#58A6FF",
        "scrollbar-background": "#0D1117",
    },
)

#: Neutral light theme — GitHub-light inspired, for bright environments.
#: Mirrors the dark theme's role-mapping: primary is the brand blue, accent
#: is a lighter interaction blue, secondary is teal for progress/commentary.
LUMEN_LIGHT = Theme(
    name="lumen-light",
    primary="#0969DA",
    secondary="#1B7C83",
    background="#F6F8FA",
    surface="#FFFFFF",
    panel="#E6EAF0",
    foreground="#1F2328",
    warning="#9A6700",
    error="#CF222E",
    success="#1A7F37",
    accent="#0550AE",
    dark=False,
    variables={
        "input-cursor-background": "#0969DA",
        "input-cursor-foreground": "#FFFFFF",
        "input-selection-background": "#0969DA 25%",
        "footer-background": "transparent",
        "scrollbar-color": "#0969DA 30%",
        "scrollbar-color-hover": "#0969DA 60%",
        "scrollbar-color-active": "#0969DA",
        "scrollbar-background": "#F6F8FA",
    },
)

#: Every theme this module knows about, in registration order.
BUILTIN_THEMES: dict[str, Theme] = {
    LUMEN_DARK.name: LUMEN_DARK,
    LUMEN_LIGHT.name: LUMEN_LIGHT,
}


def register_themes(app: App[object]) -> None:
    """Register every builtin theme on ``app`` and activate the default.

    Call from ``App.on_mount``. Idempotent: re-registering replaces the prior
    theme object of the same name (Textual's ``register_theme`` semantics).
    Accepts any ``App`` regardless of its result-type parameter.
    """

    for theme in BUILTIN_THEMES.values():
        app.register_theme(theme)
    # Activate the default last so any user override via ``app.theme = ...``
    # after this call still wins.
    app.theme = DEFAULT_THEME


__all__ = [
    "BUILTIN_THEMES",
    "DEFAULT_THEME",
    "LUMEN_DARK",
    "LUMEN_LIGHT",
    "register_themes",
]
