from __future__ import annotations

import os
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MISSING_CROPWISE_TOKEN_DETAIL = (
    "Не задан токен Cropwise. Укажите CROPWISE_API_TOKEN в .env "
    "или положите токен в uploads/.cropwise_api_token (одна строка)."
)


class Settings(BaseSettings):
    upload_dir: str = "uploads"
    svodki_root: str = "svodki"
    svodki_manual_overrides_path: str = ""
    svodki_plans_edits_path: str = ""
    svodki_season_start_date: str = ""
    svodki_combine_season_start_date: str = ""
    svodki_script_timeout_sec: int = 3600
    svodki_libreoffice_timeout_sec: int = 900
    svodki_script_retries: int = 5
    svodki_donor_retry_rounds: int = 3
    svodki_failed_report_retry_rounds: int = 2
    svodki_generation_max_workers: int = 0
    svodki_generation_auto_max_workers: int = 2
    svodki_dedicated_heavy_report_lanes: bool = True
    svodki_prefetch_concurrency: int = 20
    svodki_prefetch_mtfmi_concurrency: int = 30
    svodki_prefetch_http_retries: int = 5
    # Инкрементальное хранилище machine_tasks/mtfmi между запусками сводок (см. svodki_task_store.py).
    # Пустой путь = <upload_dir>/.task_store; выключить: svodki_task_store_enabled=false.
    svodki_task_store_enabled: bool = True
    svodki_task_store_dir: str = ""
    svodki_report22_http_concurrency: int = 15
    svodki_pdf_convert_max_workers: int = 3
    max_upload_bytes: int = 52_428_800
    cors_origins: str = "*"
    cropwise_api_token: str = ""
    cropwise_base_url: str = "https://operations.cropwise.com/api/v3"
    cropwise_season_year: int = 2026
    namolot_base_url: str = ""
    namolot_username: str = ""
    namolot_password: str = ""
    database_url: str = "postgresql+psycopg2://svodki:svodki@postgres:5432/svodki"
    log_level: str = "INFO"
    svodki_service: str = ""
    # Auth
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "svodki"
    jwt_audience: str = "svodki-web"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14
    require_email_verification: bool = False
    max_failed_login_attempts: int = 5
    lockout_minutes: int = 15
    mfa_encryption_key: str = ""
    hibp_enabled: bool = True
    bootstrap_developer_email: str = Field(
        default="svodki@chistai.ru",
        validation_alias=AliasChoices("BOOTSTRAP_DEVELOPER_EMAIL", "BOOTSTRAP_ADMIN_EMAIL"),
    )
    bootstrap_developer_password: str = Field(
        default="changeme-svodki-2026",
        validation_alias=AliasChoices("BOOTSTRAP_DEVELOPER_PASSWORD", "BOOTSTRAP_ADMIN_PASSWORD"),
    )
    bootstrap_developer_username: str = Field(
        default="developer",
        validation_alias=AliasChoices("BOOTSTRAP_DEVELOPER_USERNAME", "BOOTSTRAP_ADMIN_USERNAME"),
    )
    # Redis / Celery
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "amqp://guest:guest@rabbitmq:5672//"
    celery_result_backend: str = "redis://redis:6379/1"
    celery_enabled: bool = True
    # Пусто — все задачи в очереди "celery" (как раньше). Имя очереди (например "heavy") —
    # тяжёлые задачи (генерация, proverka) уходят в неё, а лёгкие (check_schedules)
    # остаются в "celery". Значение должно совпадать у api, beat и обоих worker.
    celery_heavy_queue: str = ""
    # Сверять «зависшие» задачи при старте worker. Выключается на лёгком worker, чтобы
    # его перезапуск не пометил работающую задачу тяжёлого worker как потерянную.
    celery_reconcile_on_start: bool = True
    auth_rate_limit_per_minute: int = 30
    # SMTP (verify / reset / proverka / schedule mail)
    smtp_host: str = Field(default="", validation_alias=AliasChoices("SMTP_HOST", "MAIL_SERVER"))
    smtp_port: int = Field(default=587, validation_alias=AliasChoices("SMTP_PORT", "MAIL_PORT"))
    smtp_user: str = Field(
        default="",
        validation_alias=AliasChoices("SMTP_USER", "SMTP_USERNAME", "MAIL_USERNAME"),
    )
    smtp_password: str = Field(
        default="",
        validation_alias=AliasChoices("SMTP_PASSWORD", "MAIL_PASSWORD"),
    )
    smtp_from: str = Field(
        default="",
        validation_alias=AliasChoices("SMTP_FROM", "SMTP_FROM_EMAIL", "MAIL_FROM"),
    )
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = Field(
        default=False,
        validation_alias=AliasChoices("SMTP_USE_SSL", "MAIL_USE_SSL"),
    )
    public_base_url: str = "http://127.0.0.1:8080"
    # Schedule (MSK cron-like, stored in DB)
    schedule_timezone: str = "Europe/Moscow"
    # Proverka: сколько часов хранить cookie Cropwise UI в мониторинге (не срок жизни _cropio_ses!)
    proverka_monitoring_cookie_ttl_hours: int = 24

    @field_validator("svodki_script_timeout_sec", "svodki_libreoffice_timeout_sec", mode="before")
    @classmethod
    def _timeout_sec(cls, v: object) -> int:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return 3600
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 3600

    @field_validator("svodki_script_retries", mode="before")
    @classmethod
    def _script_retries(cls, v: object) -> int:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return 5
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return 5

    @field_validator(
        "svodki_donor_retry_rounds",
        "svodki_failed_report_retry_rounds",
        "svodki_generation_max_workers",
        mode="before",
    )
    @classmethod
    def _int_nonneg(cls, v: object) -> int:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return 0
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0

    @field_validator("svodki_generation_auto_max_workers", "svodki_pdf_convert_max_workers", mode="before")
    @classmethod
    def _int_pos(cls, v: object) -> int:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return 3
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return 3

    @field_validator("smtp_use_tls", "smtp_use_ssl", "celery_reconcile_on_start", mode="before")
    @classmethod
    def _bool_env(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    @model_validator(mode="after")
    def _smtp_defaults(self) -> Settings:
        if self.smtp_port == 465 and not self.smtp_use_ssl:
            object.__setattr__(self, "smtp_use_ssl", True)
            object.__setattr__(self, "smtp_use_tls", False)
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()


def _upload_dir_path() -> Path:
    p = Path(settings.upload_dir)
    return p if p.is_absolute() else (Path.cwd() / p)


def get_cropwise_token() -> str:
    t = (os.getenv("CROPWISE_API_TOKEN") or settings.cropwise_api_token or "").strip()
    if t:
        return t
    path = (os.getenv("CROPWISE_API_TOKEN_FILE") or "").strip()
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
            line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
            if line:
                return line
        except OSError:
            pass
    try:
        token_file = _upload_dir_path() / ".cropwise_api_token"
        if token_file.is_file():
            raw = token_file.read_text(encoding="utf-8")
            line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
            if line:
                return line
    except OSError:
        pass
    return ""


def get_namolot_config() -> tuple[str, str, str]:
    base = (
        (os.getenv("NAMOLOT_BASE_URL") or settings.namolot_base_url or "").strip()
        or "http://10.0.1.5/hpp_elevator_new/hs/ground/report"
    )
    user = (
        (os.getenv("NAMOLOT_USERNAME") or settings.namolot_username or "").strip()
        or "Сводка"
    )
    pwd = (
        (os.getenv("NAMOLOT_PASSWORD") or settings.namolot_password or "").strip()
        or "3VzLqA6S"
    )
    return base, user, pwd


def cors_origins_list() -> list[str]:
    raw = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if raw == ["*"]:
        return ["*"]
    return raw
