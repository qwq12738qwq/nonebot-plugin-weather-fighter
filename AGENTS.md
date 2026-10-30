All data is stored in accordance with the specifications of the `nonebot_plugin_localstore` functions.
```
store.get_plugin_cache_dir()
store.get_plugin_cache_file("filename")
store.get_plugin_config_dir()
store.get_plugin_config_file("filename")
store.get_plugin_data_dir()
store.get_plugin_data_file("filename")
```
Each set of changes should correspond to a single PR submission, with the version number in `pyproject.toml` updated accordingly.