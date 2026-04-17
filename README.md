# Ollama API Proxy com Rotação de Chaves

Este é um gateway transparente para a API do Ollama (ou provedores compatíveis) que gerencia um pool de chaves API, rotacionando-as automaticamente quando limites de cota (Erro 429) são atingidos.

## Funcionalidades
- **Rotação Transparente**: O cliente recebe uma resposta 200 mesmo que a primeira chave tentada retorne 429.
- **Suporte a Streaming**: Funciona perfeitamente com `stream: true` (comum em chats).
- **Gerenciamento de Estado**: Salva o status das chaves (cooldown, requisições) em um banco SQLite (`state.db`).
- **Recuperação Automática**: Chaves em cooldown são reativadas automaticamente após o tempo configurado (default 1h).
- **Round-Robin com Prioridade**: Escolhe a próxima chave disponível respeitando a ordem de prioridade.

## Requisitos
- Python 3.11+
- Dependências listadas em `requirements.txt`

## Instalação

1. Instale as dependências:
```powershell
pip install -r requirements.txt
```

2. Configure suas chaves no arquivo `config.json`:
```json
{
  "base_url": "https://api.ollama.com",
  "keys": [
    {"id": "user-1", "api_key": "sk-...", "priority": 1},
    {"id": "user-2", "api_key": "sk-...", "priority": 2}
  ],
  "cooldown_minutes": 60,
  "max_retries": 2
}
```

## Como Executar

Inicie o servidor proxy:
```powershell
python -m uvicorn main:app --host 0.0.0.0 --port 11434
```

O proxy estará rodando em `http://localhost:11434`. Basta configurar suas ferramentas (Open WebUI, LangChain, etc.) para apontar para este endereço em vez do Ollama local.

## Testes

Para rodar a suíte de testes automatizados:
```powershell
pytest -v
```

## Estrutura do Projeto
- `main.py`: Servidor FastAPI e lógica de roteamento.
- `key_manager.py`: Gerenciamento do pool de chaves e persistência.
- `models.py`: Schemas Pydantic.
- `state.db`: Banco de dados SQLite criado automaticamente para persistir o estado das chaves.
