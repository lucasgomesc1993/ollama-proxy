import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import respx
from httpx import Response
from models import AppConfig, KeyConfig
from key_manager import KeyManager

@pytest.mark.asyncio
async def test_auto_rotate_on_429(test_client, mock_upstream):
    rr_config = AppConfig(
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
    km = KeyManager(rr_config, db_path=":memory:")
    await km.initialize()
    
    from dependencies import set_app_state
    set_app_state(rr_config, km)
    
    route = mock_upstream.post("/api/generate")
    route.side_effect = [
        Response(429, content=b"Rate limit exceeded"),
        Response(200, json={"response": "success from key2"})
    ]
    
    response = await test_client.post("/api/generate", json={"model": "llama3"})
    
    assert response.status_code == 200
    assert response.json()["response"] == "success from key2"
    
    assert route.call_count == 2
    assert km.keys[0].id == "key1"
    assert km.keys[0].status == "cooldown"


@pytest.mark.asyncio
async def test_retry_limit_reached(test_client, mock_upstream):
    route = mock_upstream.post("/api/generate").respond(429)
    
    response = await test_client.post("/api/generate", json={"model": "llama3"})
    
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_rotation_is_invisible_to_caller(test_client, mock_upstream):
    route = mock_upstream.post("/api/generate")
    route.side_effect = [
        Response(429),
        Response(200, json={"ok": True})
    ]
    
    response = await test_client.post("/api/generate", json={"model": "llama3"})
    
    assert response.status_code == 200
    assert response.json() == {"ok": True}