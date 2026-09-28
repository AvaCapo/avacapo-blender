ADDON_VERSION = (0, 1, 0)


def normalize_version(version: tuple[int, ...], *, minimum_parts: int = 3) -> tuple[int, ...]:
    """Normalize a version tuple to ensure it has at least `minimum_parts` components."""
    normalized = tuple(int(part) for part in version)
    if len(normalized) < minimum_parts:
        normalized = normalized + (0,) * (minimum_parts - len(normalized))
    return normalized


def version_to_string(version: tuple[int, ...]) -> str:
    """Convert a version tuple to a string representation."""
    if not version:
        return ""
    return ".".join(str(part) for part in normalize_version(version))


def is_version_less(current: tuple[int, ...], latest: tuple[int, ...]) -> bool:
    """Compare two version tuples to determine if the current version is less than the latest version."""
    max_parts = max(len(current), len(latest), 3)
    current_padded = normalize_version(current, minimum_parts=max_parts)
    latest_padded = normalize_version(latest, minimum_parts=max_parts)
    return current_padded < latest_padded
