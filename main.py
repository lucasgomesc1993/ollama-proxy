import asyncio
import json
import logging
import random
import re
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from models import AppConfig, AllKeysExhaustedError, KeyStatus
from key_manager import KeyManager
from dashboard import router as dashboard_router, init_usage_db, log_token_usage
from dependencies import set_app_state, get_app_config, get_key_manager

# Constants
MODEL_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_\-\.:/]+$')
MAX_MODEL_NAME_LENGTH = 128

# Logging Setup
def setup_logging(config):
    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format=config.logging.format
    )
    return logging.getLogger("ollama-proxy")

# Config Initialization
config_data = json.load(open("config.json"))
app_config = AppConfig(**config_data)
logger = setup_logging(app_config)
key_manager = KeyManager(app_config)
scheduler = AsyncIOScheduler()

# Cache for Cloud Models
MODEL_CACHE = {"data": None, "last_updated": 0}
CACHE_TTL = 900  # 15 minutes (900 seconds)

# Connection Pool Limits
CONNECTION_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0
)

# Proxy Authentication
api_key_header = APIKeyHeader(name="X-Proxy-Key", auto_error=False)


async def verify_proxy_key(request: Request, key: str = Security(api_key_header)) -> bool:
    if request.url.path == "/health":
        return True
    
    config = get_app_config()
    if not config.proxy_auth.enabled:
        return True
    
    if not key or key != config.proxy_auth.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing proxy authentication key")
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Ollama Proxy...")
    set_app_state(app_config, key_manager)
    await key_manager.initialize()
    await init_usage_db(key_manager.db_path)
    
    scheduler.add_job(key_manager.check_key_recovery, 'interval', minutes=5)
    scheduler.start()
    
    print(key_manager.get_status_summary())
    logger.info(f"Dashboard available at: http://{app_config.server.host}:{app_config.server.port}/dashboard")
    
    yield
    
    logger.info("Shutting down Ollama Proxy and saving state...")
    scheduler.shutdown()
    await key_manager.save_state()


app = FastAPI(lifespan=lifespan, title="Ollama Proxy Gateway")

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=app_config.cors.allow_origins,
    allow_credentials=True,
    allow_methods=app_config.cors.allow_methods,
    allow_headers=app_config.cors.allow_headers,
)

# Dashboard Routes
app.include_router(dashboard_router)


@app.get("/health")
async def health_check():
    km = get_key_manager()
    return {
        "status": "healthy",
        "keys": {
            "total": len(km.keys),
            "active": len([k for k in km.keys if k.status == KeyStatus.ACTIVE]),
            "cooldown": len([k for k in km.keys if k.status == KeyStatus.COOLDOWN]),
            "invalid": len([k for k in km.keys if k.status == KeyStatus.INVALID]),
        }
    }


@app.get("/api/tags")
async def get_tags(_: bool = Depends(verify_proxy_key)):
    current_time = time.time()
    
    if MODEL_CACHE["data"] and (current_time - MODEL_CACHE["last_updated"] < CACHE_TTL):
        return MODEL_CACHE["data"]
    
    try:
        config = get_app_config()
        km = get_key_manager()
        active_key = await km.get_active_key()
        headers = {"Authorization": f"Bearer {active_key.api_key.strip()}"}
        
        models_url = f"{config.base_url.rstrip('/')}/v1/models"
        timeout_cfg = config.timeouts
        
        timeout = httpx.Timeout(
            connect=timeout_cfg.connect,
            read=timeout_cfg.read,
            write=timeout_cfg.write,
            pool=timeout_cfg.pool
        )
        
        async with httpx.AsyncClient(timeout=timeout, limits=CONNECTION_LIMITS) as client:
            resp = await client.get(models_url, headers=headers)
            if resp.status_code == 200:
                cloud_models = resp.json().get("data", [])
                
                ollama_models = []
                for m in cloud_models:
                    model_id = m.get("id")
                    ollama_models.append({
                        "name": f"{model_id} (Proxy)",
                        "model": f"{model_id}",
                        "modified_at": "2024-01-01T00:00:00Z",
                        "size": 0,
                        "digest": "cloud-model",
                        "details": {
                            "parent_model": "",
                            "format": "gguf",
                            "family": "llama",
                            "families": ["llama"],
                            "parameter_size": "N/A",
                            "quantization_level": "N/A"
                        }
                    })
                
                result = {"models": ollama_models}
                MODEL_CACHE["data"] = result
                MODEL_CACHE["last_updated"] = current_time
                return result
                
    except Exception as e:
        logger.error(f"Error fetching cloud models for tag list: {e}")
    
    return MODEL_CACHE["data"] or {"models": []}


def extract_model_from_body(body: bytes) -> str:
    try:
        if body:
            data = json.loads(body)
            model = data.get("model", "unknown")
            
            if not isinstance(model, str):
                return "unknown"
            if len(model) > MAX_MODEL_NAME_LENGTH:
                logger.warning(f"Model name too long, truncating")
                model = model[:MAX_MODEL_NAME_LENGTH]
            if not MODEL_NAME_PATTERN.match(model):
                logger.warning(f"Invalid model name format: {model}")
                return "unknown"
            
            return model
    except json.JSONDecodeError:
        pass
    except Exception as e:
        logger.debug(f"Error extracting model: {e}")
    return "unknown"


def extract_usage_from_response(content: bytes) -> dict:
    try:
        data = json.loads(content)
        
        if "usage" in data:
            usage = data["usage"]
            return {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
        
        input_t = data.get("prompt_eval_count", 0)
        output_t = data.get("eval_count", 0)
        if input_t or output_t:
            return {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "total_tokens": input_t + output_t,
            }
    except json.JSONDecodeError:
        pass
    except Exception as e:
        logger.debug(f"Error parsing usage data: {e}")
    
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def extract_usage_from_stream_chunks(chunks: list) -> dict:
    total_input = 0
    total_output = 0
    
    for chunk in reversed(chunks):
        try:
            for line in chunk.decode("utf-8", errors="ignore").strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    line = line[6:]
                if not line or line == "[DONE]":
                    continue
                data = json.loads(line)
                
                if "usage" in data and data["usage"]:
                    u = data["usage"]
                    return {
                        "input_tokens": u.get("prompt_tokens", 0),
                        "output_tokens": u.get("completion_tokens", 0),
                        "total_tokens": u.get("total_tokens", 0),
                    }
                
                if data.get("done", False):
                    inp = data.get("prompt_eval_count", 0)
                    out = data.get("eval_count", 0)
                    if inp or out:
                        return {
                            "input_tokens": inp,
                            "output_tokens": out,
                            "total_tokens": inp + out,
                        }
        except Exception:
            continue
    
    return {"input_tokens": total_input, "output_tokens": total_output, "total_tokens": total_input + total_output}


async def forward_request(request: Request, path: str):
    config = get_app_config()
    km = get_key_manager()
    
    method = request.method
    body = await request.body()
    
    clean_headers = {
        "Accept": "application/json",
        "Content-Type": request.headers.get("Content-Type", "application/json"),
        "User-Agent": "Ollama/0.5.7 (Proxy; FastAPI)",
    }
    
    if "X-Request-Id" in request.headers:
        clean_headers["X-Request-Id"] = request.headers["X-Request-Id"]

    model_name = extract_model_from_body(body)
    attempts = 0
    max_retries = config.max_retries

    if config.jitter_enabled and attempts == 0:
        jitter_ms = random.randint(config.jitter_min_ms, config.jitter_max_ms)
        await asyncio.sleep(jitter_ms / 1000.0)

    timeout_cfg = config.timeouts
    stream_limits = config.stream_limits
    
    stream_timeout = httpx.Timeout(
        connect=timeout_cfg.connect,
        read=timeout_cfg.read_stream,
        write=timeout_cfg.write,
        pool=timeout_cfg.pool
    )
    normal_timeout = httpx.Timeout(
        connect=timeout_cfg.connect,
        read=timeout_cfg.read,
        write=timeout_cfg.write,
        pool=timeout_cfg.pool
    )

    is_stream_req = False
    try:
        if body:
            json_body = json.loads(body)
            is_stream_req = json_body.get("stream", False)
    except Exception:
        pass

    while attempts <= max_retries:
        try:
            active_key = await km.get_active_key(model=model_name)
            clean_headers["Authorization"] = f"Bearer {active_key.api_key.strip()}"
            
            target_url = f"{config.base_url.rstrip('/')}/{path}"
            
            logger.info(f"[{active_key.id}] [{config.rotation_mode}] Forwarding {method} to {path} (Attempt {attempts + 1})")

            if is_stream_req:
                client = httpx.AsyncClient(timeout=stream_timeout, limits=CONNECTION_LIMITS)
                try:
                    req = client.build_request(
                        method, 
                        target_url, 
                        content=body, 
                        headers=clean_headers,
                        params=request.query_params
                    )
                    response_stream = await client.send(req, stream=True)
                    
                    if response_stream.status_code == 429:
                        await response_stream.aclose()
                        await client.aclose()
                        await km.mark_quota_exceeded(active_key.id)
                        raise httpx.HTTPStatusError("Quota Exceeded", request=req, response=response_stream)
                    
                    if response_stream.status_code != 200:
                        error_body = await response_stream.aread()
                        await response_stream.aclose()
                        await client.aclose()
                        if response_stream.status_code == 401:
                            await km.mark_key_error(active_key.id, 401)
                        return Response(
                            content=error_body,
                            status_code=response_stream.status_code,
                            headers=dict(response_stream.headers)
                        )

                    collected_chunks = []
                    total_size = 0
                    current_key_id = active_key.id

                    async def stream_generator():
                        nonlocal total_size
                        try:
                            async for chunk in response_stream.aiter_bytes():
                                if len(chunk) > stream_limits.max_chunk_size:
                                    logger.error(f"Chunk too large: {len(chunk)} bytes, terminating stream")
                                    break
                                
                                if len(collected_chunks) >= stream_limits.max_collected_chunks:
                                    collected_chunks.pop(0)
                                
                                total_size += len(chunk)
                                if total_size > stream_limits.max_total_stream_size:
                                    logger.error(f"Stream exceeded max size: {total_size} bytes, terminating")
                                    break
                                
                                collected_chunks.append(chunk)
                                yield chunk
                        except httpx.ReadError as e:
                            logger.warning(f"ReadError during streaming: {e}")
                        finally:
                            await response_stream.aclose()
                            await client.aclose()
                            if collected_chunks:
                                usage = extract_usage_from_stream_chunks(collected_chunks)
                                await log_token_usage(
                                    km.db_path, current_key_id, path, model_name,
                                    usage["input_tokens"], usage["output_tokens"], usage["total_tokens"],
                                    200
                                )

                    return StreamingResponse(
                        stream_generator(),
                        status_code=response_stream.status_code,
                        headers=dict(response_stream.headers)
                    )
                except (httpx.HTTPStatusError, httpx.RequestError):
                    await client.aclose()
                    raise

            else:
                async with httpx.AsyncClient(timeout=normal_timeout, limits=CONNECTION_LIMITS) as client:
                    resp = await client.request(
                        method, 
                        target_url, 
                        content=body, 
                        headers=clean_headers,
                        params=request.query_params
                    )
                    
                    if resp.status_code == 429:
                        await km.mark_quota_exceeded(active_key.id)
                        resp.raise_for_status()
                    
                    if resp.status_code == 401:
                        await km.mark_key_error(active_key.id, 401)
                    
                    if resp.status_code == 200:
                        usage = extract_usage_from_response(resp.content)
                        await log_token_usage(
                            km.db_path, active_key.id, path, model_name,
                            usage["input_tokens"], usage["output_tokens"], usage["total_tokens"],
                            resp.status_code
                        )
                    
                    if resp.status_code >= 400:
                        logger.error(f"Upstream Error ({resp.status_code}): {resp.text}")
                    
                    return Response(
                        content=resp.content,
                        status_code=resp.status_code,
                        headers=dict(resp.headers)
                    )

        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            is_429 = getattr(exc, 'response', None) and exc.response.status_code == 429
            if is_429 or isinstance(exc, httpx.RequestError):
                attempts += 1
                if attempts <= max_retries:
                    wait_time = 0.5 * (2 ** (attempts - 1))
                    logger.warning(f"Upstream error ({exc}). Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
            
            if is_429:
                return Response(content=json.dumps({"error": "All keys exceeded quota after retries."}), status_code=429)
            raise HTTPException(status_code=502, detail=f"Error contacting Ollama API: {str(exc)}")
        
        except AllKeysExhaustedError as e:
            logger.error(f"Critical Error: {e.message} - {e.cooldown_info}")
            return Response(
                content=json.dumps({"error": e.message, "detail": e.cooldown_info}),
                status_code=503
            )

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
async def catch_all(request: Request, path: str, _: bool = Depends(verify_proxy_key)):
    return await forward_request(request, path)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=app_config.server.host, port=app_config.server.port)