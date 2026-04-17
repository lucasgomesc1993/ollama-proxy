import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime, timedelta
from models import KeyStatus, AllKeysExhaustedError, AppConfig, KeyConfig
from key_manager import KeyManager

@pytest.fixture
def rr_config():
    return AppConfig(
        base_url="https://api.test-ollama.ai",
        keys=[
            KeyConfig(id="key1", api_key="sk-1", priority=1),
            KeyConfig(id="key2", api_key="sk-2", priority=2),
            KeyConfig(id="key3", api_key="sk-3", priority=3)
        ],
        cooldown_minutes=60,
        max_retries=2,
        rotation_mode="round-robin",
        rotation_every_n=1
    )


@pytest.fixture
def failover_config():
    return AppConfig(
        base_url="https://api.test-ollama.ai",
        keys=[
            KeyConfig(id="key1", api_key="sk-1", priority=1),
            KeyConfig(id="key2", api_key="sk-2", priority=2),
            KeyConfig(id="key3", api_key="sk-3", priority=3)
        ],
        cooldown_minutes=60,
        max_retries=2,
        rotation_mode="failover"
    )


@pytest.mark.asyncio
async def test_loads_keys_from_config(failover_config):
    km = KeyManager(failover_config, db_path=":memory:")
    await km.initialize()
    assert len(km.keys) == 3
    assert all(k.status == KeyStatus.ACTIVE for k in km.keys)


@pytest.mark.asyncio
async def test_round_robin_rotation(rr_config):
    km = KeyManager(rr_config, db_path=":memory:")
    await km.initialize()
    
    k1 = await km.get_active_key()
    k2 = await km.get_active_key()
    k3 = await km.get_active_key()
    k4 = await km.get_active_key()
    
    assert k1.id == "key1"
    assert k2.id == "key2"
    assert k3.id == "key3"
    assert k4.id == "key1"


@pytest.mark.asyncio
async def test_mark_quota_exceeded(failover_config):
    km = KeyManager(failover_config, db_path=":memory:")
    await km.initialize()
    
    await km.mark_quota_exceeded("key1")
    
    k = await km.get_active_key()
    assert k.id == "key2"
    
    k = await km.get_active_key()
    assert k.id == "key2"
    
    await km.mark_quota_exceeded("key2")
    k = await km.get_active_key()
    assert k.id == "key3"


@pytest.mark.asyncio
async def test_all_keys_exhausted(failover_config):
    km = KeyManager(failover_config, db_path=":memory:")
    await km.initialize()
    
    await km.mark_quota_exceeded("key1")
    await km.mark_quota_exceeded("key2")
    await km.mark_quota_exceeded("key3")
    
    with pytest.raises(AllKeysExhaustedError) as exc:
        await km.get_active_key()
    
    assert "No API key available" in str(exc.value)
    assert "Next key available" in exc.value.cooldown_info


@pytest.mark.asyncio
async def test_key_recovery_after_cooldown(failover_config):
    km = KeyManager(failover_config, db_path=":memory:")
    await km.initialize()
    
    km.keys[0].status = KeyStatus.COOLDOWN
    km.keys[0].cooldown_until = datetime.now() - timedelta(seconds=1)
    
    await km.check_key_recovery()
    
    assert km.keys[0].status == KeyStatus.ACTIVE
    k = await km.get_active_key()
    assert k.id == "key1"


@pytest.mark.asyncio
async def test_distinguishes_401_from_429(failover_config):
    km = KeyManager(failover_config, db_path=":memory:")
    await km.initialize()
    
    await km.mark_key_error("key1", 401)
    assert km.keys[0].status == KeyStatus.INVALID
    
    await km.mark_key_error("key2", 429)
    assert km.keys[1].status == KeyStatus.COOLDOWN