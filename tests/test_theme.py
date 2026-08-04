"""Theme registration tests.

Validates that the builtin themes land on the app, the default activates on
mount, and Textual derives the right dark/light flag from the palette.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lumen.config import ConfigLoadError, load_config
from lumen.resources import ResourceManager
from lumen.ui.app import LumenApp
from lumen.ui.themes import (
    BUILTIN_THEMES,
    DEFAULT_THEME,
    LUMEN_DARK,
    LUMEN_LIGHT,
    register_themes,
)


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _make_app(tmp_path: Path, *, extra_yaml: str = "") -> LumenApp:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 1
agent:
  name: theme-test
  model:
    id: test
tools:
  builtins: []
sessions:
  directory: sessions
"""
        + extra_yaml,
        encoding="utf-8",
    )
    config = load_config(config_path)
    return LumenApp(config, ResourceManager(config, workspace=tmp_path))


class _FakeApp:
    # Minimal stand-in for App so we don't have to mount a full TUI just
    # to exercise ``register_theme``/``theme`` setters.
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}
        self.theme = "textual-dark"

    def register_theme(self, theme: object) -> None:
        name = getattr(theme, "name", "?")
        self.registered[name] = theme


def test_builtin_themes_cover_dark_and_light() -> None:
    """Both palettes are exposed under stable names."""

    assert DEFAULT_THEME == "lumen-dark"
    assert "lumen-dark" in BUILTIN_THEMES
    assert "lumen-light" in BUILTIN_THEMES
    assert LUMEN_DARK.dark is True
    assert LUMEN_LIGHT.dark is False


def test_activity_palette_is_distinct_and_readable_in_both_themes() -> None:
    """The animated run state stays orange and legible on either surface."""

    for theme in (LUMEN_DARK, LUMEN_LIGHT):
        assert theme.background is not None
        palette = theme.variables
        assert palette["activity"] != theme.foreground
        assert palette["activity-shimmer"] != palette["activity"]
        semantic_tokens = (
            "activity",
            "activity-shimmer",
            "activity-soft",
            "activity-detail",
            "tool",
            "tool-shimmer",
            "tool-soft",
            "tool-detail",
            "mode-edit",
            "mode-edit-shimmer",
            "mode-edit-soft",
            "mode-edit-detail",
            "mode-plan",
            "mode-plan-shimmer",
            "mode-plan-soft",
            "mode-plan-detail",
        )
        for token in semantic_tokens:
            assert _contrast_ratio(palette[token], theme.background) >= 4.5


def test_register_themes_activates_default() -> None:
    """``register_themes`` puts every theme on the app and sets the default."""

    host = _FakeApp()
    register_themes(host)  # type: ignore[arg-type]
    assert set(host.registered) == {"lumen-dark", "lumen-light"}
    assert host.theme == DEFAULT_THEME


def test_register_themes_activates_requested_theme() -> None:
    """An explicit theme name wins; unknown names fall back to the default."""

    host = _FakeApp()
    register_themes(host, theme="lumen-light")  # type: ignore[arg-type]
    assert host.theme == "lumen-light"
    register_themes(host, theme="not-a-theme")  # type: ignore[arg-type]
    assert host.theme == DEFAULT_THEME


def test_ui_theme_config_validates_registered_names(tmp_path: Path) -> None:
    """``ui.theme`` defaults to dark, accepts registered names, rejects others."""

    base_yaml = """
version: 1
agent:
  model:
    id: test
tools:
  builtins: []
"""
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(base_yaml, encoding="utf-8")
    assert load_config(config_path).ui.theme == "lumen-dark"

    config_path.write_text(base_yaml + "ui:\n  theme: lumen-light\n", encoding="utf-8")
    assert load_config(config_path).ui.theme == "lumen-light"

    config_path.write_text(base_yaml + "ui:\n  theme: solarized\n", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match=r"ui\.theme must be one of"):
        load_config(config_path)


async def test_app_default_theme_is_loop_dark(tmp_path: Path) -> None:
    """After mount the running app reports the lumen-dark theme."""

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # The default theme is set in on_mount; ``app.theme`` reflects the
        # currently-active theme name after registration runs.
        assert app.theme == "lumen-dark"
        # The dark flag is read from the active theme object.
        assert app.current_theme.dark is True


async def test_app_uses_configured_theme_at_startup(tmp_path: Path) -> None:
    """``ui.theme`` from the config selects the active theme on mount."""

    app = _make_app(tmp_path, extra_yaml="ui:\n  theme: lumen-light\n")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "lumen-light"
        assert app.current_theme.dark is False


async def test_theme_command_lists_switches_and_validates(tmp_path: Path) -> None:
    """``/theme`` lists themes with the active one marked, switches live, and
    reports a clear error for unknown names without changing the theme."""

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        shown: list[str] = []

        async def capture(message: str) -> None:
            shown.append(message)

        app._append_system = capture  # type: ignore[method-assign]

        await app._handle_command("/theme")
        listing = shown[-1]
        assert "* lumen-dark" in listing
        assert "  lumen-light" in listing

        await app._handle_command("/theme lumen-light")
        assert app.theme == "lumen-light"
        assert app.current_theme.dark is False
        assert shown[-1] == "Switched to lumen-light."

        await app._handle_command("/theme lumen-light")
        assert shown[-1] == "Already on lumen-light."

        await app._handle_command("/theme solarized")
        assert app.theme == "lumen-light"
        assert "Unknown theme 'solarized'" in shown[-1]
