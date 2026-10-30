"""Request-local state that reaches the model without changing the stable system prefix."""


def set_temporary_context(request, marker: str, text: str) -> None:
    """Replace only our marked temporary part; preserve other plugins and real user text."""
    parts = []
    for part in getattr(request, "extra_user_content_parts", None) or []:
        temporary = part.get("_no_save", False) if isinstance(part, dict) else getattr(
            part, "_no_save", False
        )
        body = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
        if not (temporary and isinstance(body, str) and body.startswith(marker)):
            parts.append(part)
    if text:
        parts.append({"type": "text", "text": marker + "\n" + text, "_no_save": True})
    request.extra_user_content_parts = parts
