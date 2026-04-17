import json
import os
import uuid

CONFIG_FILE = "config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"base_url": "https://api.ollama.com", "keys": [], "cooldown_minutes": 60, "max_retries": 2}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def main():
    clear_screen()
    print("=== Gerenciador de Chaves do Ollama Proxy ===\n")
    config = load_config()
    keys = config.get("keys", [])
    
    print(f"Atualmente, há {len(keys)} chave(s) configurada(s).")
    
    api_key = input("\nDigite a nova API Key (ou deixe em branco para cancelar): ").strip()
    if not api_key:
        print("Operação cancelada.")
        return
        
    # Verificar se a chave ja existe
    for k in keys:
        if k.get("api_key") == api_key:
            print("\nEssa chave já está configurada!")
            return

    # Definir id único (ou baseado nas chaves atuais) e prioridade
    new_id = f"key-{len(keys) + 1}-{uuid.uuid4().hex[:4]}"
    
    priority_input = input("Prioridade da chave (1 é a maior prioridade). Pressione ENTER para padrão: ").strip()
    priority = int(priority_input) if priority_input.isdigit() else len(keys) + 1

    keys.append({
        "id": new_id,
        "api_key": api_key,
        "priority": priority
    })
    
    # Ordenar por prioridade para se manter consistente no config
    keys.sort(key=lambda x: x.get("priority", 999))
    
    config["keys"] = keys
    save_config(config)
    
    print(f"\n✅ Sucesso! A nova chave foi adicionada com ID '{new_id}'.")
    print(f"Total de chaves configuradas: {len(keys)}")

if __name__ == "__main__":
    main()
