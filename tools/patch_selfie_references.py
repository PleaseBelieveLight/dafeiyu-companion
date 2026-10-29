"""Patch both pinned selfie entry points to use request-local scene references."""

from pathlib import Path


IMPORT = "from astrbot.core.utils.selfie_reference import select_selfie_reference\n"
SELECTION = (
    "                selfie_reference = select_selfie_reference(\n"
    '                    self.plugin_config, user_input, getattr(self, "data_dir", "")\n'
    "                )\n"
)


def apply_selfie_references(target: Path) -> None:
    path = target / "astrbot/data/plugins/astrbot_plugin_omnidraw/main.py"
    text = path.read_text(encoding="utf-8-sig")
    marker = "# Dafeiyu Windows: request-local scene references.\n"
    if marker in text:
        if text.count(IMPORT) != 1 or text.count("selfie_reference.prompt_suffix") != 2:
            raise RuntimeError("The scene reference patch is incomplete")
        return

    replacements = [
        ("from .constants import (\n", IMPORT + "\nfrom .constants import (\n"),
        (
            '            fallback_action = action or "看着镜头微笑"\n',
            '            fallback_action = action or "看着镜头微笑"\n'
            "            selfie_reference = select_selfie_reference(\n"
            '                self.plugin_config, fallback_action, getattr(self, "data_dir", "")\n'
            "            )\n",
        ),
        (
            "                    final_prompt, kwargs = self.persona_manager.build_persona_prompt(optimized_action)\n",
            "                    final_prompt, kwargs = self.persona_manager.build_persona_prompt(optimized_action)\n"
            "                    final_prompt += selfie_reference.prompt_suffix\n"
            "                    if selfie_reference.images:\n"
            '                        kwargs["persona_ref"] = selfie_reference.images[0]\n',
        ),
        (
            "                extra_kwargs.update(kwargs)\n\n                raw_refs = self._get_event_images(event)\n",
            SELECTION
            + "                final_prompt += selfie_reference.prompt_suffix\n"
            + "                if selfie_reference.images:\n"
            + '                    extra_kwargs["persona_ref"] = selfie_reference.images[0]\n'
            + "                extra_kwargs.update(kwargs)\n\n                raw_refs = self._get_event_images(event)\n",
        ),
    ]
    for old, new in replacements:
        if text.count(old) != 1:
            raise RuntimeError(
                "The pinned selfie handler no longer matches its scene reference patch"
            )
        text = text.replace(old, new, 1)
    old = "target_refs = raw_refs if raw_refs else self.plugin_config.persona_ref_images"
    if text.count(old) != 2:
        raise RuntimeError("The pinned selfie reference branches changed")
    text = text.replace(
        old, "target_refs = raw_refs if raw_refs else list(selfie_reference.images)"
    )
    path.write_text(marker + text, encoding="utf-8")
