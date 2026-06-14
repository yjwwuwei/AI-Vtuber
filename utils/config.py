import json
from pathlib import Path

class Config:
    # 单例模式
    # _instance = None
    config = None

    # def __new__(cls, *args, **kwargs):
    #     if not cls._instance:
    #         cls._instance = super(Config, cls).__new__(cls)  # 不再传递 *args, **kwargs
    #     return cls._instance

    def __init__(self, config_file):
        if self.config is None:
            config_path = Path(config_file)
            with open(config_path, 'r', encoding="utf-8") as f:
                self.config = json.load(f)

            local_config_path = config_path.with_name(f"{config_path.stem}.local{config_path.suffix}")
            if local_config_path.exists():
                with open(local_config_path, 'r', encoding="utf-8") as f:
                    local_config = json.load(f)
                self.config = self._deep_merge(self.config, local_config)
    
    def __getitem__(self, key):
        return self.config.get(key)
    
    def get(self, *keys):
        result = self.config
        for key in keys:
            result = result.get(key, None)
            if result is None:
                break
        return result

    def _deep_merge(self, base, override):
        if not isinstance(base, dict) or not isinstance(override, dict):
            return override

        merged = dict(base)
        for key, value in override.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = self._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged
