"""Warm-neutral themes shared by Lumen's terminal interaction surfaces."""

from __future__ import annotations

from textual.app import App
from textual.theme import Theme

#: The theme name set on app start (``app.theme = DEFAULT_THEME``).
DEFAULT_THEME = "lumen-dark"

#: Warm charcoal rather than blue-black. One amber family carries interaction
#: state; success and error colors are reserved for semantic outcomes.
LUMEN_DARK = Theme(
    name=DEFAULT_THEME,
    primary="#C99552",
    secondary="#B7A28B",
    background="#181816",
    surface="#22211F",
    panel="#2B2926",
    foreground="#ECE9E4",
    warning="#D6A552",
    error="#D16D75",
    success="#86A66C",
    accent="#D7A15D",
    dark=True,
    variables={
        # Dedicated activity ramp. Claude Code treats its spinner and shimmer
        # as a paired state, rather than letting activity inherit muted body
        # text. Keep this narrowly scoped so the rest of the amber system
        # remains calm while an active run is unmistakably alive.
        "activity": "#F0A24A",
        "activity-shimmer": "#FFC166",
        "activity-soft": "#C77B2A",
        "activity-detail": "#D8A56B",
        "activity-meta": "#948A80",
        # Semantic accents used sparingly in transcript and mode chrome.
        # Orange remains the active-agent color; lavender identifies tools,
        # teal identifies edits, and green identifies read-only planning.
        "tool": "#C7ACE8",
        "tool-shimmer": "#E0C9FF",
        "tool-soft": "#9E82C5",
        "tool-detail": "#BCA8D1",
        "mode-edit": "#69B9AF",
        "mode-edit-shimmer": "#8BD2C8",
        "mode-edit-soft": "#468E87",
        "mode-edit-detail": "#8FBFB9",
        "mode-plan": "#93B97A",
        "mode-plan-shimmer": "#B5D89B",
        "mode-plan-soft": "#6F9458",
        "mode-plan-detail": "#A4BD91",
        "mode-auto": "#F0A24A",
        "mode-manual": "#948A80",
        # Match the prompt editor's cursor to the primary so the focus state
        # reads as part of the theme rather than a Textual default.
        "input-cursor-background": "#C99552",
        "input-cursor-foreground": "#181816",
        "input-selection-background": "#C99552 28%",
        "footer-background": "transparent",
        # Scrollbars tinted with primary so the 1-cell chrome reads as themed.
        "scrollbar-color": "#C99552 25%",
        "scrollbar-color-hover": "#C99552 55%",
        "scrollbar-color-active": "#C99552",
        "scrollbar-background": "#181816",
    },
)

#: Matches the warm off-white and amber language used by Lumen's web client.
LUMEN_LIGHT = Theme(
    name="lumen-light",
    primary="#A06D24",
    # 4.34:1 on background — metadata-only, never body text (see the note on
    # the activity-meta variable below).
    secondary="#81766E",
    background="#FDFDFC",
    surface="#F5F5F3",
    panel="#EFEFEC",
    foreground="#242422",
    warning="#A06D24",
    error="#A63F4D",
    # Deepened from #5F8C69 (3.79:1 on background) to clear WCAG AA 4.5:1
    # for text-sized status glyphs; #4F7A57 measures 4.85:1 on #FDFDFC.
    success="#4F7A57",
    accent="#8F5E1C",
    dark=False,
    variables={
        # On a light surface, clarity comes from a deeper chromatic orange
        # rather than the higher luminance used by the dark theme.
        "activity": "#A95813",
        "activity-shimmer": "#B95A0E",
        "activity-soft": "#914718",
        "activity-detail": "#8F6030",
        # #81766E measures 4.34:1 on background — just under the 4.5:1 body
        # bar. It is reserved for secondary metadata (elapsed time, tool
        # counts, inactive mode labels), never for body text, so it stays.
        "activity-meta": "#81766E",
        "tool": "#76539C",
        "tool-shimmer": "#824FAF",
        "tool-soft": "#5E417E",
        "tool-detail": "#806A92",
        "mode-edit": "#267B75",
        "mode-edit-shimmer": "#197970",
        "mode-edit-soft": "#1F625D",
        "mode-edit-detail": "#4C7774",
        "mode-plan": "#527B40",
        "mode-plan-shimmer": "#3F7929",
        "mode-plan-soft": "#3E6131",
        "mode-plan-detail": "#64795A",
        "mode-auto": "#A95813",
        "mode-manual": "#81766E",
        "input-cursor-background": "#A06D24",
        "input-cursor-foreground": "#FFFFFF",
        "input-selection-background": "#A06D24 22%",
        "footer-background": "transparent",
        "scrollbar-color": "#A06D24 22%",
        "scrollbar-color-hover": "#A06D24 48%",
        "scrollbar-color-active": "#A06D24",
        "scrollbar-background": "#FDFDFC",
    },
)

#: Every theme this module knows about, in registration order.
BUILTIN_THEMES: dict[str, Theme] = {
    LUMEN_DARK.name: LUMEN_DARK,
    LUMEN_LIGHT.name: LUMEN_LIGHT,
}

#: Last-resort colors for :func:`theme_color` when the active theme lacks a
#: token (e.g. a user-registered custom theme). Values mirror ``LUMEN_DARK``
#: so a missing token degrades to the default theme's look instead of an
#: arbitrary Rich default. Centralized so call sites never hardcode dark hex
#: literals; with the builtin themes this path is never exercised.
FALLBACK_COLORS: dict[str, str] = {
    "primary": "#C99552",
    "foreground": "#ECE9E4",
    "success": "#86A66C",
    "error": "#D16D75",
    "activity": "#F0A24A",
    "activity-shimmer": "#FFC166",
    "activity-soft": "#C77B2A",
    "activity-detail": "#D8A56B",
    "activity-meta": "#948A80",
    "tool": "#C7ACE8",
    "mode-edit": "#69B9AF",
    "mode-manual": "#948A80",
}


def theme_color(app: App[object], token: str, fallback: str) -> str:
    """Resolve a semantic variable or builtin color from the active theme."""

    theme = app.current_theme
    color = theme.variables.get(token)
    if not color and "-" not in token:
        color = getattr(theme, token, None)
    return color if isinstance(color, str) and color else fallback


def register_themes(app: App[object], *, theme: str | None = None) -> None:
    """Register every builtin theme on ``app`` and activate one of them.

    Call from ``App.on_mount``. Idempotent: re-registering replaces the prior
    theme object of the same name (Textual's ``register_theme`` semantics).
    ``theme`` selects the active theme (typically ``config.ui.theme``); an
    unknown or omitted name falls back to :data:`DEFAULT_THEME`.
    Accepts any ``App`` regardless of its result-type parameter.
    """

    for builtin in BUILTIN_THEMES.values():
        app.register_theme(builtin)
    # Activate last so any user override via ``app.theme = ...`` after this
    # call still wins.
    app.theme = theme if theme in BUILTIN_THEMES else DEFAULT_THEME


__all__ = [
    "BUILTIN_THEMES",
    "DEFAULT_THEME",
    "FALLBACK_COLORS",
    "LUMEN_DARK",
    "LUMEN_LIGHT",
    "register_themes",
    "theme_color",
]
