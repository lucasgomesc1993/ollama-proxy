import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import json

@pytest.mark.asyncio
async def test_successful_request_passthrough(test_client, mock_upstream):
    mock_upstream.post("/api/generate").respond(200, json={"response": "ok"})
    
    response = await test_client.post("/api/generate", json={"model": "llama3", "prompt": "hi"})
    
    assert response.status_code == 200
    assert response.json() == {"response": "ok"}
    assert "Authorization" in mock_upstream.calls.last.request.headers
    assert mock_upstream.calls.last.request.headers["Authorization"] == "Bearer sk-1"

@pytest.mark.asyncio
async def test_streaming_response(test_client, mock_upstream):
    async def delta_stream():
        yield b'{"response": "part1"}\n'
        yield b'{"response": "part2"}\n'

    mock_upstream.post("/api/chat").respond(200, content=delta_stream())
    
    response = await test_client.post("/api/chat", json={"model": "llama3", "stream": True})
    
    assert response.status_code == 200
    full_content = b""
    async for chunk in response.aiter_bytes():
        full_content += chunk
    
    assert b"part1" in full_content
    assert b"part2" in full_content

@pytest.mark.asyncio
async def test_proxy_headers_cleaned(test_client, mock_upstream):
    mock_upstream.post("/api/generate").respond(200, json={})
    
    await test_client.post("/api/generate", json={"model": "llama3"}, headers={
        "X-Test-Header": "custom-value",
        "Authorization": "Bearer original-token"
    })
    
    sent_request = mock_upstream.calls.last.request
    assert "X-Test-Header" not in sent_request.headers
    assert sent_request.headers["Authorization"] == "Bearer sk-1"

@pytest.mark.asyncio
async def test_unknown_route_forwarded(test_client, mock_upstream):
    mock_upstream.get("/something/else").respond(200, text="hello")
    
    response = await test_client.get("/something/else")
    
    assert response.status_code == 200
    assert response.text == "hello"

@pytest.mark.asyncio
async def test_health_check(test_client):
    response = await test_client.get("/health")
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "keys" in data
    assert data["keys"]["total"] == 3
    assert data["keys"]["active"] == 3