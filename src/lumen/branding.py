"""Stable public identity for the Lumen framework."""

FRAMEWORK_NAME = "Lumen"
FRAMEWORK_SLUG = "lumen"


def product_label(agent_name: str) -> str:
    """Return a public framework label without repeating the default name."""

    normalized = agent_name.strip()
    if not normalized or normalized.casefold() == FRAMEWORK_NAME.casefold():
        return FRAMEWORK_NAME
    return f"{FRAMEWORK_NAME} / {normalized}"


__all__ = ["FRAMEWORK_NAME", "FRAMEWORK_SLUG", "product_label"]
