"""Замечания по культуре для агронома. Гектары сводки не меняют."""
from __future__ import annotations

_CROP_CODES = ("CROP_NO_HISTORY", "CROP_UNRESOLVED", "CROP_FIELD", "CROP_SPLIT")


def collect_data_checks() -> list[str]:
    """Культура не найдена, чужое поле, два crop_id на поле и сезон задания ≠ год культуры."""
    notes: list[str] = []
    try:
        import svodki_sanity

        summary = svodki_sanity.summary()
        for code in _CROP_CODES:
            item = (summary.get("codes") or {}).get(code)
            if not item:
                continue
            examples = "; ".join(item.get("examples") or [])
            text = (
                f"{item.get('text')}: {item.get('count')} шт, {float(item.get('ha') or 0):.1f} га"
            )
            if examples:
                text = f"{text} ({examples})"
            notes.append(text)
    except Exception:
        pass
    try:
        from season_mismatch_log import mismatch_lines

        for line in mismatch_lines():
            notes.append(f"год культуры не совпал с сезоном задания: {line}")
    except Exception:
        pass
    try:
        from mtfmi_batch_fetch import history_load_errors

        notes.extend(history_load_errors())
    except Exception:
        pass
    return notes
