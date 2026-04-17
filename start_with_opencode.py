import os
import sys
import json
import time
import subprocess
import threading
from pathlib import Path

def start_proxy():
    print("Iniciando o Proxy Ollama...")
    # Executa o uvicorn em background
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "11434"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

def configure_opencode():
    print("Configurando OpenCode para usar o Proxy Ollama...")
    # Caminho do config do OpenCode no Windows e Mac/Linux
    user_home = Path(os.path.expanduser("~"))
    config_dir = user_home / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    
    config_file = config_dir / "opencode.json"
    
    # Configuração padrao do OpenCode
    config_data = {}
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception:
            pass
            
    if "provider" not in config_data:
        config_data["provider"] = {}
        
    # Tenta buscar os modelos dinamicamente do Proxy
    print("Buscando lista de modelos do Proxy...")
    dynamic_models = {}
    try:
        import urllib.request
        import json as json_lib
        # Timeout curto pois o proxy deve estar ligando
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as response:
            tags_data = json_lib.loads(response.read().decode())
            for m in tags_data.get("models", []):
                # O ID interno do modelo (sem o sufixo Proxy)
                model_id = m.get("model")
                # O nome amigável que aparecerá no dropdown
                display_name = m.get("name")
                dynamic_models[model_id] = {"name": display_name}
    except Exception as e:
        print(f"Aviso: Não foi possível buscar modelos dinâmicos ({e}). Usando padrão.")
        # Fallback se o proxy ainda não estiver pronto ou falhar
        dynamic_models = {
            "llama3": {"name": "Llama 3 (Proxy)"},
            "qwen2.5-coder": {"name": "Qwen 2.5 Coder (Proxy)"},
            "glm-5": {"name": "GLM-5 (Proxy)"}
        }

    # Adicionamos/Atualizamos o provider do Ollama apontando para o nosso proxy local
    config_data["provider"]["ollama"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Ollama Proxy (Local)",
        "options": {
            "baseURL": "http://localhost:11434/v1",
            "headers": {
                "Authorization": "Bearer dummy-key-proxy"
            }
        },
        "models": dynamic_models
    }
    
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)
    print(f"OpenCode configurado com {len(dynamic_models)} modelos!")

def run_opencode():
    # Verifica se opencode está instalado
    import shutil
    if shutil.which("opencode") is None:
        print("OpenCode CLI não encontrado. Instalando via npm...")
        subprocess.run("npm install -g opencode-ai", shell=True)
        
    print("Iniciando OpenCode Agent...")
    try:
        # Usa os.system ou subprocess interativo para abrir o CLI
        subprocess.run("opencode", shell=True)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    # 1. Inicia o Proxy primeiro para que possamos consultá-lo
    proxy_proc = start_proxy()
    
    # 2. Aguarda o proxy inicializar o banco e o servidor
    print("Aguardando inicialização do Proxy...")
    time.sleep(4) 
    
    try:
        # 3. Configura o OpenCode buscando os modelos do proxy que já está ativo
        configure_opencode()
        
        # 4. Inicia o agente
        run_opencode()
    finally:
        print("\nEncerrando o Proxy Ollama...")
        proxy_proc.terminate()
        proxy_proc.wait()
