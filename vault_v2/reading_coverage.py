"""Shared wording for locally prepared, bounded document text."""


def coverage_warning(
    read_chars: int, total_chars: int | None, *, extracted: bool = False,
) -> str | None:
    if total_chars is None or read_chars >= total_chars:
        return None
    read_count = f"{read_chars:,}".replace(",", " ")
    total_count = f"{total_chars:,}".replace(",", " ")
    unit = "extracted characters" if extracted else "characters"
    return f"read {read_count} of {total_count} {unit} — the rest was not checked"
