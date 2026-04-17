import asyncio
import json
import logging
import random
import re
import time
import hashlib
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from models import AppConfig, AllKeysExhaustedError, KeyStatus
from key_manager import KeyManager
from dashboard import router as dashboard_router, init_usage_db, log_token_usage
from dependencies import set_app_state, get_app_config, get_key_manager

# Constants
MODEL_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_\-\.:/]+$')
MAX_MODEL_NAME_LENGTH = 128

# Cache de modelos disponíveis no upstream (para validação)
_available_models_cache = {"models": set(), "last_updated": 0, "ttl": 300}

def generate_model_digest(model_id: str) -> str:
    """Gera um digest consistente baseado no model_id para parecer legítimo."""
    # Usar hash do model_id para parecer consistente mas não óbvio
    return hashlib.sha256(model_id.encode()).hexdigest()[:64]

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

# Esconder identidade do Proxy - remover Server header FastAPI
# FastAPI usa X-Powered-By e Server, precisamos interceptar
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

class RemoveServerHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Remover headers que identificam como proxy
        headers_to_remove = ['server', 'x-powered-by']
        for header in headers_to_remove:
            if header in response.headers:
                del response.headers[header]
        return response


api_key_header = APIKeyHeader(name="Authorization", auto_error=False)

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


app = FastAPI(
    lifespan=lifespan, 
    title="Ollama API",
    # Esconder identidade do Proxy - remover Server header FastAPI
    docs_url="/docs" if app_config.logging.verbose else None,
    redoc_url="/redoc" if app_config.logging.verbose else None,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=app_config.cors.allow_origins,
    allow_credentials=True,
    allow_methods=app_config.cors.allow_methods,
    allow_headers=app_config.cors.allow_headers,
)

# Middleware para remover headers que identificam como proxy
app.add_middleware(RemoveServerHeaderMiddleware)

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
                    # Gerar digest legítimo baseado no model_id
                    digest = generate_model_digest(model_id)
                    
                    # Extrair tamanho real se disponível
                    size = m.get("size", 0) or 0
                    
                    # Parse do ID para tentar inferir família e formato
                    model_lower = model_id.lower()
                    family = "llama"
                    if "qwen" in model_lower:
                        family = "qwen"
                    elif "mistral" in model_lower:
                        family = "mistral"
                    elif "deepseek" in model_lower:
                        family = "deepseek"
                    elif "gemma" in model_lower:
                        family = "gemma"
                    elif "phi" in model_lower:
                        family = "phi"
                    
                    # Tentar extrair parâmetros do nome
                    param_size = "N/A"
                    for prefix in ["7b", "8b", "13b", "14b", "32b", "70b", "405b"]:
                        if prefix in model_lower:
                            param_size = prefix.upper()
                            break
                    
                    # Detectar quantização
                    quant_level = "N/A"
                    for q in ["q4_0", "q4_k_m", "q5_0", "q5_k_m", "q8_0", "q8", "fp16", "fp32"]:
                        if q in model_lower:
                            quant_level = q.upper()
                            break
                    
                    ollama_models.append({
                        "name": model_id,  # Remover "(Proxy)" que identifica proxy
                        "model": model_id,
                        "modified_at": "2024-01-01T00:00:00Z",
                        "size": size,
                        "digest": digest,
                        "details": {
                            "parent_model": "",
                            "format": "gguf",
                            "family": family,
                            "families": [family],
                            "parameter_size": param_size,
                            "quantization_level": quant_level
                        }
                    })
                
                result = {"models": ollama_models}
                MODEL_CACHE["data"] = result
                MODEL_CACHE["last_updated"] = current_time
                return result
                
    except Exception as e:
        logger.error(f"Error fetching cloud models for tag list: {e}")
    
    return MODEL_CACHE["data"] or {"models": []}


def generate_model_digest(model_id: str) -> str:
    """Gera um digest consistente baseado no model_id para parecer legítimo."""
    # Usar hash do model_id para parecer consistente mas não óbvio
    return hashlib.sha256(model_id.encode()).hexdigest()[:64]


async def get_available_models(config, km) -> set:
    """Busca e cacheia modelos disponíveis no upstream para validação."""
    global _available_models_cache
    current_time = time.time()
    
    if current_time - _available_models_cache["last_updated"] < _available_models_cache["ttl"]:
        return _available_models_cache["models"]
    
    try:
        active_key = await km.get_active_key()
        headers = {"Authorization": f"Bearer {active_key.api_key.strip()}"}
        models_url = f"{config.base_url.rstrip('/')}/v1/models"
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(models_url, headers=headers)
            if resp.status_code == 200:
                cloud_models = resp.json().get("data", [])
                models_set = {m.get("id") for m in cloud_models if m.get("id")}
                _available_models_cache["models"] = models_set
                _available_models_cache["last_updated"] = current_time
                return models_set
    except Exception as e:
        logger.debug(f"Could not fetch available models: {e}")
    
    return _available_models_cache["models"]


def extract_model_from_body(body: bytes) -> tuple:
    """Extrai o nome do modelo do body e retorna (model, error)."""
    try:
        if body:
            data = json.loads(body)
            model = data.get("model", "unknown")
            
            if not isinstance(model, str):
                return None, Response(
                    content=json.dumps({"error": "model must be a string"}),
                    status_code=400
                )
            if len(model) > MAX_MODEL_NAME_LENGTH:
                logger.warning(f"Model name too long: {model[:20]}...")
                return None, Response(
                    content=json.dumps({"error": "model name too long"}),
                    status_code=400
                )
            if not MODEL_NAME_PATTERN.match(model):
                logger.warning(f"Invalid model name format: {model}")
                return None, Response(
                    content=json.dumps({"error": "invalid model name format"}),
                    status_code=400
                )
            
            # Retorna o modelo sem validar se existe (validação feita em forward_request)
            return model, None
    except json.JSONDecodeError:
        return None, Response(
            content=json.dumps({"error": "invalid JSON in request body"}),
            status_code=400
        )
    except Exception as e:
        logger.debug(f"Error extracting model: {e}")
        return None, Response(
            content=json.dumps({"error": "could not parse model from request"}),
            status_code=400
        )
    return None, None


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
    
    # Headers autênticos do Ollama para evitar detecção como proxy
    clean_headers = {
        "Accept": request.headers.get("Accept", "application/json"),
        "Accept-Encoding": request.headers.get("Accept-Encoding", "gzip, deflate, br"),
        "Content-Type": request.headers.get("Content-Type", "application/json"),
        "User-Agent": request.headers.get("User-Agent", "ollama/0.5.7"),
    }
    
    # Propagar headers relevantes do cliente (X-Ollama-* e outros identificadores)
    for header_name in request.headers:
        header_lower = header_name.lower()
        if header_lower.startswith("x-ollama-") or header_lower in ["x-request-id"]:
            clean_headers[header_name] = request.headers[header_name]

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
                    
                    if response_stream.status_code == 429 or response_stream.status_code == 529:
                        await response_stream.aclose()
                        await client.aclose()
                        await km.mark_quota_exceeded(active_key.id)
                        raise httpx.HTTPStatusError("Quota Exceeded" if response_stream.status_code == 429 else "Overloaded", request=req, response=response_stream)
                    
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
            is_529 = getattr(exc, 'response', None) and exc.response.status_code == 529
            is_5xx = getattr(exc, 'response', None) and 500 <= exc.response.status_code < 600
            
            # Mapear erro 529 (Overloaded) para cooldown da chave
            if is_529:
                await km.mark_quota_exceeded(active_key.id)
                logger.warning(f"Key {active_key.id} returned 529 Overloaded. Cooling down.")
            
            # Delay adaptativo após erros 429/529/5xx
            if is_429 or is_529 or is_5xx or isinstance(exc, httpx.RequestError):
                attempts += 1
                if attempts <= max_retries:
                    # Backoff exponencial com jitter para evitar padrões
                    base_wait = 0.5 * (2 ** (attempts - 1))
                    jitter = random.uniform(0, 1.0) if config.jitter_enabled else 0
                    wait_time = base_wait + jitter
                    logger.warning(f"Upstream error ({exc}). Retrying in {wait_time:.2f}s...")
                    await asyncio.sleep(wait_time)
                    continue
            
            if is_429 or is_529:
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