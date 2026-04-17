import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import pytest_asyncio
import aiosqlite
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient
from key_manager import KeyManager
from models import AppConfig, KeyConfig
from dependencies import set_app_state, clear_app_state
from dashboard import init_usage_db
import respx


@pytest.fixture
def mock_config():
    return AppConfig(
        base_url="https://api.test-ollama.ai",
        keys=[
            KeyConfig(id="key1", api_key="sk-1", priority=1),
            KeyConfig(id="key2", api_key="sk-2", priority=2),
            KeyConfig(id="key3", api_key="sk-3", priority=3)
        ],
        cooldown_minutes=60,
        max_retries=2,
        proxy_auth={"enabled": False, "api_key": ""},
        rotation_mode="failover"
    )


@pytest_asyncio.fixture
async def km(mock_config):
    manager = KeyManager(mock_config, db_path=":memory:")
    await manager.initialize()
    return manager


@pytest_asyncio.fixture(autouse=True)
async def setup_app_state(mock_config, km):
    set_app_state(mock_config, km)
    
    # Create in-memory token_usage table for tests
    test_conn = await aiosqlite.connect(":memory:")
    await test_conn.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT NOT NULL,
            endpoint TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            status_code INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await test_conn.commit()
    
    # Patch log_token_usage to use our test connection
    async def patched_log_token_usage(db_path, key_id, endpoint, model, 
                                       input_tokens, output_tokens, total_tokens, status_code):
        await test_conn.execute("""
            INSERT INTO token_usage (key_id, endpoint, model, input_tokens, output_tokens, total_tokens, status_code, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (key_id, endpoint, model, input_tokens, output_tokens, total_tokens, status_code))
        await test_conn.commit()
    
    with patch('dashboard.log_token_usage', patched_log_token_usage):
        with patch('main.log_token_usage', patched_log_token_usage):
            yield
    
    await test_conn.close()
    clear_app_state()


@pytest_asyncio.fixture
async def test_client():
    from main import app
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_upstream():
    with respx.mock(base_url="https://api.test-ollama.ai", assert_all_called=False) as respx_mock:
        yield respx_mock