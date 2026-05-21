from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List
import os


class Settings(BaseSettings):
    # Anthropic
    anthropic_api_key: str = ""

    # Polymarket
    polymarket_api_key: str = ""
    polymarket_private_key: str = ""
    polymarket_base_url: str = "https://clob.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Kalshi
    kalshi_api_key: str = ""
    kalshi_email: str = ""
    kalshi_password: str = ""
    kalshi_base_url: str = "https://trading-api.kalshi.com/trade-api/v2"

    # Social
    twitter_bearer_token: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "TradingBot/1.0"

    # Database
    database_url: str = "postgresql+asyncpg://trader:trader@localhost:5432/tradingbot"
    redis_url: str = "redis://localhost:6379/0"

    # App
    secret_key: str = "dev-secret-key-change-in-production"
    debug: bool = False
    log_level: str = "INFO"
    allowed_origins: str = "http://localhost:3000"

    # Risk Controls
    max_daily_loss_usd: float = 500.0
    max_position_size_usd: float = 200.0
    max_portfolio_exposure_usd: float = 2000.0
    min_edge_threshold: float = 0.05
    min_liquidity_usd: float = 10000.0
    min_confidence_score: float = 0.65

    @property
    def origins_list(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
