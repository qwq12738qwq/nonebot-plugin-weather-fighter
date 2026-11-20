from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOCALSTORE_DATA_DIR", tempfile.mkdtemp(prefix="wf-test-"))

import nonebot

nonebot.init()
nonebot.load_plugin("nonebot_plugin_weather_fighter")
