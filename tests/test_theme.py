"""Theme registration tests.

Validates that the builtin themes land on the app, the default activates on
mount, and Textual derives the right dark/light flag from the palette.
"""

from __future__ import annotations

from pathlib import Path

from lumen.config import load_config
from lumen.resources import ResourceManager
from lumen.ui.app import LumenApp
from lumen.ui.themes import (
    BUILTIN_THEMES,
    DEFAULT_THEME,
    LUMEN_DARK,
    LUMEN_LIGHT,
    register_themes,
)


def _make_app(tmp_path: Path) -> LumenApp:
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
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    return LumenApp(config, ResourceManager(config, workspace=tmp_path))


def test_builtin_themes_cover_dark_and_light() -> None:
    """Both palettes are exposed under stable names."""

    assert DEFAULT_THEME == "lumen-dark"
    assert "lumen-dark" in BUILTIN_THEMES
    assert "lumen-light" in BUILTIN_THEMES
    assert LUMEN_DARK.dark is True
    assert LUMEN_LIGHT.dark is False


def test_register_themes_activates_default() -> None:
    """``register_themes`` puts every theme on the app and sets the default."""

    class Host:
        # Minimal stand-in for App so we don't have to mount a full TUI just
        # to exercise ``register_theme``/``theme`` setters.
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}
            self.theme = "textual-dark"

        def register_theme(self, theme: object) -> None:
            name = getattr(theme, "name", "?")
            self.registered[name] = theme

    host = Host()
    register_themes(host)  # type: ignore[arg-type]
    assert set(host.registered) == {"lumen-dark", "lumen-light"}
    assert host.theme == DEFAULT_THEME


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
