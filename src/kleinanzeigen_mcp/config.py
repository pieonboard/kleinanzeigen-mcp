"""Validated configuration; importing this module reads no credentials."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    transport: Literal["stdio", "streamable-http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    token_path: Path = Field(
        default_factory=lambda: Path("~/.kleinanzeigen_api/token.json").expanduser()
    )
    user_id: str | None = Field(default=None, pattern=r"^[0-9]{1,32}$")
    contact_name: str | None = Field(default=None, min_length=1, max_length=200)
    request_interval: float = Field(default=1.5, ge=1.5)
    request_timeout: float = Field(default=25, gt=0)
    operation_timeout: float = Field(default=180, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    enable_analysis: bool = False
    upload_roots: tuple[Path, ...] = ()

    @model_validator(mode="after")
    def validate_settings(self) -> Self:
        if not ipaddress.ip_address(self.host).is_loopback:
            raise ValueError("Only loopback HTTP binds are supported")
        if self.operation_timeout < self.request_timeout:
            raise ValueError("Operation timeout must cover request timeout")
        return self

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Self:
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}
        for field in cls.model_fields:
            key = "KA_" + field.upper()
            if key in env:
                values[field] = env[key]
        token = env.get("KA_TOKEN_PATH")
        if token is None and env.get("KLEINANZEIGEN_TOKEN_DIR"):
            token = str(Path(env["KLEINANZEIGEN_TOKEN_DIR"]) / "token.json")
        if token is not None:
            values["token_path"] = Path(token).expanduser().resolve()
        if "KA_UPLOAD_ROOTS" in env:
            values["upload_roots"] = tuple(
                Path(p).expanduser().resolve() for p in env["KA_UPLOAD_ROOTS"].split(":") if p
            )
        return cls.model_validate(values)
