from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOCALSTORE_DATA_DIR", tempfile.mkdtemp(prefix="wf-test-"))
os.environ.setdefault("LOCALSTORE_CACHE_DIR", tempfile.mkdtemp(prefix="wf-test-cache-"))

import nonebot

nonebot.init()
nonebot.load_plugin("nonebot_plugin_weather_fighter")
