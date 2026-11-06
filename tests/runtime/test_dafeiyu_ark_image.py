"""Verify Ark image results can be attached without downloading a storage URL."""

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

from PIL import Image

from data.plugins.astrbot_plugin_omnidraw.main import OmniDrawPlugin
from data.plugins.astrbot_plugin_omnidraw.models import ProviderConfig
from data.plugins.astrbot_plugin_omnidraw.providers.openai_impl import OpenAIProvider


ARK_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/images/generations"


class ArkImageTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, endpoint, data):
        request = MagicMock()
        request.__aenter__.return_value = SimpleNamespace(
            status=200,
            read=AsyncMock(return_value=json.dumps({"data": [data]}).encode()),
        )
        session = SimpleNamespace(post=Mock(return_value=request))
        config = ProviderConfig("selfie", "openai_image", endpoint, ["test-key"], "test-model", 10)
        return OpenAIProvider(config, session)

    async def test_ark_returns_inline_jpeg_and_saves_it_for_delivery(self):
        with io.BytesIO() as buffer:
            Image.new("RGB", (8, 8)).save(buffer, format="JPEG")
            photo = buffer.getvalue()
        encoded = base64.b64encode(photo).decode()
        provider = self.provider(ARK_ENDPOINT, {"b64_json": encoded})
        provider.load_reference_images = AsyncMock(return_value=[(photo, "image/jpeg")])

        result = await provider.generate_image("test selfie", user_refs=["reference.jpg"])

        provider.session.post.assert_called_once()
        request = provider.session.post.call_args
        self.assertEqual(request.args[0], ARK_ENDPOINT)
        self.assertEqual(request.kwargs["json"]["response_format"], "b64_json")
        self.assertEqual(request.kwargs["json"]["image"], f"data:image/jpeg;base64,{encoded}")
        with tempfile.TemporaryDirectory() as folder:
            plugin = SimpleNamespace(data_dir=folder, _prune_cache_if_needed=Mock())
            image = OmniDrawPlugin._create_image_component(plugin, result)
            local_path = Path(image.path)
            self.assertEqual(local_path.suffix, ".jpg")
            self.assertEqual(local_path.read_bytes(), photo)

    async def test_other_provider_defaults_and_explicit_ark_format_are_preserved(self):
        image_url = "https://example.invalid/generated.png"
        cases = [
            ("https://example.invalid/v1/images/generations", {}, None),
            (ARK_ENDPOINT, {"response_format": "url"}, "url"),
            (ARK_ENDPOINT, {}, "b64_json"),
        ]
        for endpoint, kwargs, expected in cases:
            with self.subTest(endpoint=endpoint, kwargs=kwargs):
                provider = self.provider(endpoint, {"url": image_url})
                self.assertEqual(await provider.generate_image("test image", **kwargs), image_url)
                payload = provider.session.post.call_args.kwargs["json"]
                self.assertEqual(payload.get("response_format"), expected)
