"""Settings read once from the environment or `.env`."""

from functools import cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Every tunable value, with a default that runs the demo."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    openai_api_key: SecretStr | None = None
    model: str = Field("gpt-6.1-sol", description="Model that plans, calls tools and writes answers")
    judge_model: str = Field("gpt-6-luna", description="Model that checks each claim against its evidence")
    max_steps: int = Field(
        12,
        description="Model calls the agent may make in one turn before it must answer",
    )
    rewrites: int = Field(1, description="Times an answer that fails the evidence check is sent back")
    data: Path = Field(
        ROOT / "data" / "telemetry.jsonl",
        description="Telemetry snapshots, one JSON object per line",
    )
    database: Path = Field(
        ROOT / "copilot.sqlite",
        description="Audit log and executed actions",
    )
    checkpoints: Path = Field(ROOT / "checkpoints.sqlite", description="Conversation state saved after every step")

    low_disk_used: float = Field(90.0, description="Percent of the system volume used that counts as low on disk")
    high_memory_used: float = Field(85.0, description="Percent of RAM used that counts as memory pressure")
    consistent_share: float = Field(
        0.8,
        description="Share of a device's snapshots that makes a condition consistent",
    )
    battery_cycle_limit: int = Field(1000, description="Charge cycles at which a battery counts as near end of life")
    evidence_days: int = Field(3, description="Days a condition must persist before an action may cite it")
    page_rows: int = Field(25, description="Device or finding rows a list tool returns per page, after its count row")

    eval_concurrency: int = Field(4, description="Eval cases run at once")
    prices: dict[str, tuple[float, float, float]] = Field(
        {"gpt-6.1-sol": (2.0, 0.10, 10.0), "gpt-6-luna": (0.10, 0.01, 0.50)},
        description="USD per million input, cached input and output tokens, for the eval's cost column",
    )


@cache
def settings() -> Settings:
    """The process-wide settings."""
    return Settings()
