from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Reads IF_API_KEY, IF_MOCK, IF_HOST, IF_PORT from .env or the environment."""

    api_key: str = ""
    base_url: str = "https://api.infiniteflight.com/public/v2"
    mock: bool = False
    host: str = "127.0.0.1"
    port: int = 8000

    # OpenWeatherMap key (env: IF_OWM_API_KEY). Powers the destination weather
    # panel and the cloud/wind/precip/temperature map overlays. Free tier is fine.
    # Left blank -> weather features degrade gracefully instead of erroring.
    owm_api_key: str = ""

    # Cache TTLs (seconds). The Live API asks you not to hammer it; these keep a
    # page full of viewers down to one upstream request per interval.
    ttl_sessions: int = 300
    ttl_flights: int = 12
    ttl_atc: int = 30
    ttl_world: int = 60
    ttl_route: int = 30
    ttl_static: int = 3600  # aircraft + livery catalogues barely change
    ttl_wx: int = 600       # weather changes slowly; 10 min keeps OWM calls low
    ttl_photo: int = 86400  # a city's photo doesn't change day to day
    ttl_tile: int = 1800    # weather tiles


    model_config = SettingsConfigDict(env_file=".env", env_prefix="IF_", extra="ignore")


settings = Settings()
