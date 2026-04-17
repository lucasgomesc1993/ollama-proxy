from typing import Optional
from models import AppConfig
from key_manager import KeyManager


_app_config: Optional[AppConfig] = None
_key_manager: Optional[KeyManager] = None


def set_app_state(config: AppConfig, key_manager: KeyManager) -> None:
    global _app_config, _key_manager
    _app_config = config
    _key_manager = key_manager


def get_app_config() -> AppConfig:
    if _app_config is None:
        raise RuntimeError("Application not initialized. Call set_app_state first.")
    return _app_config


def get_key_manager() -> KeyManager:
    if _key_manager is None:
        raise RuntimeError("Application not initialized. Call set_app_state first.")
    return _key_manager


def clear_app_state() -> None:
    global _app_config, _key_manager
    _app_config = None
    _key_manager = None