from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from enum import Enum


class KeyStatus(str, Enum):
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    INVALID = "invalid"


class KeyConfig(BaseModel):
    id: str
    api_key: str
    priority: int = 1


class ProxyAuthConfig(BaseModel):
    enabled: bool = False
    api_key: str = ""


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 11434


class TimeoutConfig(BaseModel):
    connect: float = 30.0
    read: float = 120.0
    read_stream: Optional[float] = None
    write: float = 30.0
    pool: float = 30.0


class CORSConfig(BaseModel):
    allow_origins: List[str] = Field(default_factory=lambda: ["http://localhost:3000", "http://localhost:11434"])
    allow_methods: List[str] = Field(default_factory=lambda: ["*"])
    allow_headers: List[str] = Field(default_factory=lambda: ["*"])


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    file: Optional[str] = None


class StreamLimitsConfig(BaseModel):
    max_collected_chunks: int = 1000
    max_chunk_size: int = 10 * 1024 * 1024
    max_total_stream_size: int = 100 * 1024 * 1024


class AppConfig(BaseModel):
    base_url: str
    keys: List[KeyConfig]
    cooldown_minutes: int = 60
    max_retries: int = 2
    rotation_mode: str = "failover"
    rotation_every_n: int = 5
    rate_limit_per_minute: int = 15
    jitter_enabled: bool = True
    jitter_min_ms: int = 200
    jitter_max_ms: int = 1500
    session_sticky_minutes: int = 5
    proxy_auth: ProxyAuthConfig = ProxyAuthConfig()
    server: ServerConfig = ServerConfig()
    timeouts: TimeoutConfig = TimeoutConfig()
    cors: CORSConfig = CORSConfig()
    logging: LoggingConfig = LoggingConfig()
    stream_limits: StreamLimitsConfig = StreamLimitsConfig()


class KeyState(BaseModel):
    id: str
    api_key: str
    status: KeyStatus = KeyStatus.ACTIVE
    cooldown_until: Optional[datetime] = None
    request_count: int = 0
    last_used: Optional[datetime] = None
    priority: int = 1


class AllKeysExhaustedError(Exception):
    def __init__(self, message: str, cooldown_info: str):
        self.message = message
        self.cooldown_info = cooldown_info
        super().__init__(self.message)