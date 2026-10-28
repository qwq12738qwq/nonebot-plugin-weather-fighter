from __future__ import annotations

from pydantic import BaseModel, Field


class Config(BaseModel):
    """插件配置；环境变量前缀见 .env.example。"""

    weather_fighter_bucket_bars: int = Field(default=1800, ge=60)
    weather_fighter_bucket_realtime: int = Field(default=300, ge=30)
    weather_fighter_bucket_hourly: int = Field(default=3600, ge=60)
    weather_fighter_bucket_daily: int = Field(default=10800, ge=300)
    weather_fighter_stale_seconds: int = Field(default=6 * 3600, ge=60)
    weather_fighter_http_timeout: float = Field(default=30.0, gt=0)
    weather_fighter_retry_count: int = Field(default=3, ge=1, le=10)
    weather_fighter_preload_season_series: bool = True
    weather_fighter_caiyun_token: str = ""
    weather_fighter_command_cooldown: float = Field(default=5.0, ge=0)
    weather_fighter_principal: float = Field(default=10_000.0, ge=1_000)
    weather_fighter_leverage: float = Field(default=100.0, ge=1, le=100)
    weather_fighter_safety_entry_pct: float = Field(default=20.0, ge=1, le=100)
    weather_fighter_safety_grant_pct: float = Field(default=5.0, gt=0, le=100)
    weather_fighter_safety_target_pct: float = Field(default=35.0, gt=0, le=100)
    weather_fighter_chart_bars: int = Field(default=64, ge=8, le=1000)
    weather_fighter_enable_region: bool = True
    weather_fighter_default_province: str = "河北"
