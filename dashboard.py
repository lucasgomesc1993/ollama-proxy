"""
Ollama Proxy Monitoring Dashboard.
Displays token usage, estimated costs, and savings per key.
"""
import aiosqlite
import json
import aiofiles
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, HTTPException, Depends, Security
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader

router = APIRouter()

COMMERCIAL_PRICES = {
    "input_per_1m": 5.00,
    "output_per_1m": 15.00,
}

api_key_header = APIKeyHeader(name="X-Proxy-Key", auto_error=False)


async def verify_dashboard_access(key: str = Security(api_key_header)) -> bool:
    from dependencies import get_app_config
    config = get_app_config()
    
    if not config.proxy_auth.enabled:
        return True
    
    if not key or key != config.proxy_auth.api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


async def init_usage_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
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
        await db.commit()


async def log_token_usage(db_path: str, key_id: str, endpoint: str, model: str,
                          input_tokens: int, output_tokens: int, total_tokens: int,
                          status_code: int):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            INSERT INTO token_usage (key_id, endpoint, model, input_tokens, output_tokens, total_tokens, status_code, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (key_id, endpoint, model, input_tokens, output_tokens, total_tokens, status_code,
              datetime.now().isoformat()))
        await db.commit()


async def get_dashboard_data(db_path: str) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("""
            SELECT 
                COUNT(*) as total_requests,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(total_tokens), 0) as total_tokens
            FROM token_usage WHERE status_code = 200
        """) as cursor:
            row = await cursor.fetchone()
            totals = {
                "total_requests": row[0],
                "total_input": row[1],
                "total_output": row[2],
                "total_tokens": row[3],
            }

        async with db.execute("""
            SELECT 
                key_id,
                COUNT(*) as requests,
                COALESCE(SUM(input_tokens), 0) as input_tokens,
                COALESCE(SUM(output_tokens), 0) as output_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                MAX(timestamp) as last_used
            FROM token_usage WHERE status_code = 200
            GROUP BY key_id ORDER BY total_tokens DESC
        """) as cursor:
            keys_data = []
            async for row in cursor:
                i_cost = (row["input_tokens"] / 1_000_000) * COMMERCIAL_PRICES["input_per_1m"]
                o_cost = (row["output_tokens"] / 1_000_000) * COMMERCIAL_PRICES["output_per_1m"]
                keys_data.append({
                    "key_id": row[0],
                    "requests": row[1],
                    "input_tokens": row[2],
                    "output_tokens": row[3],
                    "total_tokens": row[4],
                    "last_used": row[5],
                    "cost_saved": round(i_cost + o_cost, 4)
                })

        async with db.execute("""
            SELECT 
                COALESCE(model, 'unknown') as model,
                COUNT(*) as requests,
                COALESCE(SUM(input_tokens), 0) as input_tokens,
                COALESCE(SUM(output_tokens), 0) as output_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens
            FROM token_usage WHERE status_code = 200
            GROUP BY model ORDER BY total_tokens DESC
        """) as cursor:
            models_data = []
            async for row in cursor:
                i_cost = (row["input_tokens"] / 1_000_000) * COMMERCIAL_PRICES["input_per_1m"]
                o_cost = (row["output_tokens"] / 1_000_000) * COMMERCIAL_PRICES["output_per_1m"]
                models_data.append({
                    "model": row[0],
                    "requests": row[1],
                    "input_tokens": row[2],
                    "output_tokens": row[3],
                    "total_tokens": row[4],
                    "cost_saved": round(i_cost + o_cost, 4)
                })

        async with db.execute("""
            SELECT key_id, model, endpoint, input_tokens, output_tokens, total_tokens, status_code, timestamp
            FROM token_usage 
            ORDER BY timestamp DESC LIMIT 20
        """) as cursor:
            recent_activity = []
            async for row in cursor:
                recent_activity.append(dict(row))

        async with db.execute("""
            SELECT 
                strftime('%Y-%m-%d %H:00', timestamp) as hour,
                COUNT(*) as requests,
                COALESCE(SUM(total_tokens), 0) as tokens
            FROM token_usage 
            WHERE timestamp >= datetime('now', '-24 hours') AND status_code = 200
            GROUP BY hour ORDER BY hour
        """) as cursor:
            timeline = []
            async for row in cursor:
                timeline.append({
                    "hour": row[0],
                    "requests": row[1],
                    "tokens": row[2],
                })

        input_cost = (totals["total_input"] / 1_000_000) * COMMERCIAL_PRICES["input_per_1m"]
        output_cost = (totals["total_output"] / 1_000_000) * COMMERCIAL_PRICES["output_per_1m"]
        total_savings = input_cost + output_cost

        return {
            "totals": totals,
            "savings": {
                "input_cost_saved": round(input_cost, 4),
                "output_cost_saved": round(output_cost, 4),
                "total_saved": round(total_savings, 4),
            },
            "keys": keys_data,
            "models": models_data,
            "timeline": timeline,
            "recent_activity": recent_activity
        }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(_: bool = Depends(verify_dashboard_access)):
    return DASHBOARD_HTML


@router.get("/api/dashboard/data")
async def dashboard_api(_: bool = Depends(verify_dashboard_access)):
    from dependencies import get_key_manager
    key_manager = get_key_manager()
    data = await get_dashboard_data(key_manager.db_path)
    data["key_status"] = []
    for k in key_manager.keys:
        data["key_status"].append({
            "id": k.id,
            "status": k.status.value,
            "cooldown_until": k.cooldown_until.isoformat() if k.cooldown_until else None,
            "request_count": k.request_count,
            "priority": k.priority,
        })
    return data


@router.post("/api/keys")
async def add_key_api(request: Request, _: bool = Depends(verify_dashboard_access)):
    from dependencies import get_key_manager
    key_manager = get_key_manager()
    try:
        data = await request.json()
        await key_manager.add_key(data)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/keys/{key_id}")
async def delete_key_api(key_id: str, _: bool = Depends(verify_dashboard_access)):
    from dependencies import get_key_manager
    key_manager = get_key_manager()
    try:
        await key_manager.delete_key(key_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/config")
async def get_config_api(_: bool = Depends(verify_dashboard_access)):
    from dependencies import get_app_config
    app_config = get_app_config()
    return {
        "rotation_mode": app_config.rotation_mode,
        "rotation_every_n": app_config.rotation_every_n,
        "rate_limit_per_minute": app_config.rate_limit_per_minute,
        "jitter_enabled": app_config.jitter_enabled,
        "jitter_min_ms": app_config.jitter_min_ms,
        "jitter_max_ms": app_config.jitter_max_ms,
        "session_sticky_minutes": app_config.session_sticky_minutes,
    }


@router.put("/api/config")
async def update_config_api(request: Request, _: bool = Depends(verify_dashboard_access)):
    from dependencies import get_app_config, get_key_manager
    app_config = get_app_config()
    key_manager = get_key_manager()
    
    try:
        data = await request.json()
        
        if "rotation_mode" in data and data["rotation_mode"] in ("failover", "round-robin", "session-sticky"):
            app_config.rotation_mode = data["rotation_mode"]
        if "rotation_every_n" in data:
            app_config.rotation_every_n = max(1, int(data["rotation_every_n"]))
        if "rate_limit_per_minute" in data:
            app_config.rate_limit_per_minute = max(0, int(data["rate_limit_per_minute"]))
        if "jitter_enabled" in data:
            app_config.jitter_enabled = bool(data["jitter_enabled"])
        if "jitter_min_ms" in data:
            app_config.jitter_min_ms = max(0, int(data["jitter_min_ms"]))
        if "jitter_max_ms" in data:
            app_config.jitter_max_ms = max(app_config.jitter_min_ms, int(data["jitter_max_ms"]))
        if "session_sticky_minutes" in data:
            app_config.session_sticky_minutes = max(1, int(data["session_sticky_minutes"]))
        
        key_manager.config = app_config
        
        async with aiofiles.open("config.json", "w", encoding="utf-8") as f:
            await f.write(json.dumps(app_config.model_dump(), indent=2))
        
        return {"success": True, "config": {
            "rotation_mode": app_config.rotation_mode,
            "rotation_every_n": app_config.rotation_every_n,
            "rate_limit_per_minute": app_config.rate_limit_per_minute,
            "jitter_enabled": app_config.jitter_enabled,
            "jitter_min_ms": app_config.jitter_min_ms,
            "jitter_max_ms": app_config.jitter_max_ms,
            "session_sticky_minutes": app_config.session_sticky_minutes,
        }}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ollama Proxy — Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
    <style>
        *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-card: #16161f;
            --bg-card-hover: #1c1c28;
            --border: #1f1f2e;
            --text-primary: #e8e8ef;
            --text-secondary: #8888a0;
            --text-muted: #555570;
            --accent-green: #10b981;
            --accent-green-glow: rgba(16, 185, 129, 0.15);
            --accent-blue: #3b82f6;
            --accent-blue-glow: rgba(59, 130, 246, 0.15);
            --accent-purple: #8b5cf6;
            --accent-purple-glow: rgba(139, 92, 246, 0.15);
            --accent-amber: #f59e0b;
            --accent-amber-glow: rgba(245, 158, 11, 0.15);
            --accent-rose: #f43f5e;
            --accent-rose-glow: rgba(244, 63, 94, 0.15);
            --accent-cyan: #06b6d4;
            --radius: 16px;
            --radius-sm: 10px;
        }

        body {
            font-family: 'Inter', -apple-system, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            overflow-x: hidden;
        }

        body::before {
            content: '';
            position: fixed;
            top: -200px; left: -200px;
            width: 600px; height: 600px;
            background: radial-gradient(circle, rgba(139,92,246,0.06) 0%, transparent 70%);
            pointer-events: none;
            z-index: 0;
        }
        body::after {
            content: '';
            position: fixed;
            bottom: -200px; right: -200px;
            width: 600px; height: 600px;
            background: radial-gradient(circle, rgba(16,185,129,0.05) 0%, transparent 70%);
            pointer-events: none;
            z-index: 0;
        }

        .app { position: relative; z-index: 1; max-width: 1400px; margin: 0 auto; padding: 32px 24px; }

        .header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 40px;
        }
        .header-left { display: flex; align-items: center; gap: 16px; }
        .logo {
            width: 48px; height: 48px;
            background: linear-gradient(135deg, var(--accent-purple), var(--accent-blue));
            border-radius: 14px;
            display: flex; align-items: center; justify-content: center;
            font-size: 22px; font-weight: 800; color: #fff;
            box-shadow: 0 4px 24px rgba(139,92,246,0.3);
        }
        .header h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }
        .header p { color: var(--text-secondary); font-size: 14px; margin-top: 2px; }
        
        .header-actions { display: flex; gap: 12px; }
        
        button {
            background: var(--accent-purple);
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: var(--radius-sm);
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        button:hover { background: #7c4dff; transform: translateY(-1px); }
        button:active { transform: translateY(0); }
        button.secondary {
            background: rgba(255,255,255,0.05);
            border: 1px solid var(--border);
        }
        button.secondary:hover { background: rgba(255,255,255,0.08); }
        button.danger {
            background: rgba(244, 63, 94, 0.1);
            color: var(--accent-rose);
            border: 1px solid rgba(244, 63, 94, 0.2);
            padding: 6px 12px;
            font-size: 12px;
        }
        button.danger:hover { background: var(--accent-rose); color: white; }

        .add-key-form {
            display: none;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 24px;
            margin-bottom: 32px;
            animation: slideDown 0.3s ease-out;
        }
        @keyframes slideDown {
            from { opacity: 0; transform: translateY(-10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .form-grid {
            display: grid;
            grid-template-columns: 1fr 2fr 100px auto;
            gap: 16px;
            align-items: flex-end;
        }
        .input-group { display: flex; flex-direction: column; gap: 8px; }
        .input-group label { font-size: 12px; font-weight: 600; color: var(--text-secondary); }
        input, select {
            background: rgba(0,0,0,0.2);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            padding: 10px 14px;
            color: white;
            font-size: 14px;
            outline: none;
        }
        input:focus, select:focus { border-color: var(--accent-purple); }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 16px;
            margin-bottom: 32px;
        }
        .stat-card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 24px;
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }
        .stat-card:hover {
            background: var(--bg-card-hover);
            border-color: rgba(255,255,255,0.06);
            transform: translateY(-2px);
        }
        .stat-card .glow {
            position: absolute; top: 0; left: 0; right: 0; height: 1px;
            opacity: 0.6;
        }
        .stat-card .icon {
            width: 40px; height: 40px; border-radius: 10px;
            display: flex; align-items: center; justify-content: center;
            font-size: 18px; margin-bottom: 16px;
        }
        .stat-card .label { font-size: 13px; color: var(--text-secondary); font-weight: 500; margin-bottom: 8px; }
        .stat-card .value { font-size: 32px; font-weight: 800; letter-spacing: -1px; line-height: 1; }
        .stat-card .sub { font-size: 12px; color: var(--text-muted); margin-top: 8px; }

        .green .glow { background: linear-gradient(90deg, transparent, var(--accent-green), transparent); }
        .green .icon { background: var(--accent-green-glow); color: var(--accent-green); }
        .green .value { color: var(--accent-green); }

        .blue .glow { background: linear-gradient(90deg, transparent, var(--accent-blue), transparent); }
        .blue .icon { background: var(--accent-blue-glow); color: var(--accent-blue); }

        .purple .glow { background: linear-gradient(90deg, transparent, var(--accent-purple), transparent); }
        .purple .icon { background: var(--accent-purple-glow); color: var(--accent-purple); }

        .amber .glow { background: linear-gradient(90deg, transparent, var(--accent-amber), transparent); }
        .amber .icon { background: var(--accent-amber-glow); color: var(--accent-amber); }
        .amber .value { color: var(--accent-amber); }

        .section-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 32px;
        }
        @media (max-width: 1000px) { .section-grid { grid-template-columns: 1fr; } }

        .panel {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 24px;
        }
        .panel-header {
            display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 20px;
        }
        .panel-title { font-size: 16px; font-weight: 700; display: flex; align-items: center; gap: 8px; }

        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th {
            text-align: left; padding: 10px 14px;
            font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px;
            color: var(--text-muted); font-weight: 600;
            border-bottom: 1px solid var(--border);
        }
        td {
            padding: 12px 14px; font-size: 13px;
            border-bottom: 1px solid rgba(255,255,255,0.03);
            font-variant-numeric: tabular-nums;
        }
        tr:hover td { background: rgba(255,255,255,0.015); }

        .key-id {
            display: inline-flex; align-items: center; gap: 8px;
            font-weight: 600; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px;
        }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; }
        .status-active { background: var(--accent-green); box-shadow: 0 0 6px var(--accent-green); }
        .status-cooldown { background: var(--accent-amber); box-shadow: 0 0 6px var(--accent-amber); }
        .status-invalid { background: var(--accent-rose); box-shadow: 0 0 6px var(--accent-rose); }

        .token-bar {
            height: 6px; border-radius: 3px; background: rgba(255,255,255,0.05);
            overflow: hidden; min-width: 80px;
        }
        .token-bar-fill {
            height: 100%;
            border-radius: 3px;
            background: linear-gradient(90deg, var(--accent-blue), var(--accent-purple));
            transition: width 0.6s ease;
        }

        .cost-tag {
            background: var(--accent-green-glow);
            color: var(--accent-green);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 700;
        }

        .chart-container { position: relative; height: 260px; }

        .savings-banner {
            background: linear-gradient(135deg, rgba(16,185,129,0.08), rgba(6,182,212,0.05));
            border: 1px solid rgba(16,185,129,0.15);
            border-radius: var(--radius);
            padding: 28px 32px;
            display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 32px;
            flex-wrap: wrap; gap: 20px;
        }
        .savings-left h3 { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
        .savings-left p { color: var(--text-secondary); font-size: 13px; }
        .savings-amount {
            font-size: 42px; font-weight: 900; letter-spacing: -2px;
            background: linear-gradient(135deg, var(--accent-green), var(--accent-cyan));
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .savings-detail { color: var(--text-secondary); font-size: 12px; margin-top: 4px; text-align: right; }

        .full-width { grid-column: 1 / -1; }
        
        .method-tag {
            font-size: 10px; font-weight: 800; padding: 2px 5px; border-radius: 4px;
            background: rgba(255,255,255,0.08); color: var(--text-secondary);
        }
        .status-badge {
            font-size: 11px; font-weight: 700; padding: 2px 6px; border-radius: 10px;
        }
        .status-200 { background: var(--accent-green-glow); color: var(--accent-green); }
        .status-429 { background: var(--accent-amber-glow); color: var(--accent-amber); }
        .status-401, .status-500 { background: var(--accent-rose-glow); color: var(--accent-rose); }
        
        .timestamp { color: var(--text-muted); font-size: 11px; }

        .footer { text-align: center; padding: 32px 0; color: var(--text-muted); font-size: 12px; }

        #toast {
            position: fixed; bottom: 24px; right: 24px;
            background: var(--bg-card); border: 1px solid var(--border);
            padding: 12px 24px; border-radius: var(--radius-sm);
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
            z-index: 1000; display: none;
            animation: slideIn 0.3s ease-out;
        }
        @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
    </style>
</head>
<body>
    <div class="app">
        <header class="header">
            <div class="header-left">
                <div class="logo">⚡</div>
                <div>
                    <h1>Ollama Proxy</h1>
                    <p>Token Monitoring & Savings</p>
                </div>
            </div>
            <div class="header-actions">
                <button id="toggleFormBtn" class="secondary">+ Add Key</button>
            </div>
        </header>

        <div id="addKeyForm" class="add-key-form">
            <div class="form-grid">
                <div class="input-group">
                    <label>Key ID</label>
                    <input type="text" id="newId" placeholder="e.g., cloud-primary">
                </div>
                <div class="input-group">
                    <label>API Key</label>
                    <input type="password" id="newKey" placeholder="sk-...">
                </div>
                <div class="input-group">
                    <label>Priority</label>
                    <input type="number" id="newPriority" value="1" min="1">
                </div>
                <button onclick="addKey()">Save Key</button>
            </div>
        </div>

        <div id="content"></div>

        <footer class="footer">Ollama Proxy Gateway — Auto-refresh every 15s</footer>
    </div>

    <div id="toast"></div>

    <script>
        function fmt(n) {
            if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
            if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
            return n.toLocaleString('en-US');
        }
        function money(n) { return '$' + n.toFixed(2); }
        function timeAgo(dateStr) {
            const date = new Date(dateStr);
            return date.toLocaleTimeString('en-US');
        }

        let timelineChart = null;
        let isFormVisible = false;
        let currentConfig = {};

        document.getElementById('toggleFormBtn').onclick = () => {
            isFormVisible = !isFormVisible;
            document.getElementById('addKeyForm').style.display = isFormVisible ? 'block' : 'none';
        };

        async function toast(msg, color = 'var(--accent-purple)') {
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.style.borderLeft = '4px solid ' + color;
            t.style.display = 'block';
            setTimeout(() => { t.style.display = 'none'; }, 3000);
        }

        async function addKey() {
            const id = document.getElementById('newId').value;
            const api_key = document.getElementById('newKey').value;
            const priority = parseInt(document.getElementById('newPriority').value);

            if (!id || !api_key) return toast('Fill all fields!', 'var(--accent-rose)');

            try {
                const res = await fetch('/api/keys', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id, api_key, priority })
                });
                if (res.ok) {
                    toast('Key added successfully!', 'var(--accent-green)');
                    document.getElementById('newId').value = '';
                    document.getElementById('newKey').value = '';
                    document.getElementById('addKeyForm').style.display = 'none';
                    isFormVisible = false;
                    loadData();
                } else {
                    const err = await res.json();
                    toast(err.detail || 'Error adding key', 'var(--accent-rose)');
                }
            } catch (e) {
                toast('Connection error', 'var(--accent-rose)');
            }
        }

        async function deleteKey(id) {
            if (!confirm('Are you sure you want to delete key "' + id + '"?')) return;

            try {
                const res = await fetch('/api/keys/' + id, { method: 'DELETE' });
                if (res.ok) {
                    toast('Key removed.', 'var(--accent-amber)');
                    loadData();
                } else {
                    toast('Error removing key', 'var(--accent-rose)');
                }
            } catch (e) {
                toast('Connection error', 'var(--accent-rose)');
            }
        }

        async function loadData() {
            try {
                const res = await fetch('/api/dashboard/data');
                const data = await res.json();
                render(data);
            } catch (e) {
                console.error('Error:', e);
            }
        }

        function render(data) {
            const t = data.totals;
            const s = data.savings;
            const activeKeys = data.key_status.filter(k => k.status === 'active').length;
            const totalKeys = data.key_status.length;

            document.getElementById('content').innerHTML = 
                '<div class="savings-banner">' +
                    '<div class="savings-left">' +
                        '<h3>Total Savings</h3>' +
                        '<p>Cost saved using local/pooled keys vs. commercial APIs</p>' +
                    '</div>' +
                    '<div>' +
                        '<div class="savings-amount">' + money(s.total_saved) + '</div>' +
                        '<div class="savings-detail">In: ' + money(s.input_cost_saved) + ' · Out: ' + money(s.output_cost_saved) + '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="stats-grid">' +
                    '<div class="stat-card blue"><div class="glow"></div><div class="icon">📊</div><div class="label">Requests</div><div class="value">' + fmt(t.total_requests) + '</div><div class="sub">success (HTTP 200)</div></div>' +
                    '<div class="stat-card purple"><div class="glow"></div><div class="icon">🔤</div><div class="label">Total Tokens</div><div class="value">' + fmt(t.total_tokens) + '</div><div class="sub">In: ' + fmt(t.total_input) + ' · Out: ' + fmt(t.total_output) + '</div></div>' +
                    '<div class="stat-card green"><div class="glow"></div><div class="icon">🔑</div><div class="label">Active Keys</div><div class="value">' + activeKeys + '/' + totalKeys + '</div><div class="sub">in rotation pool</div></div>' +
                    '<div class="stat-card amber"><div class="glow"></div><div class="icon">⚡</div><div class="label">Est. Savings</div><div class="value">' + money(s.total_saved) + '</div><div class="sub">vs. avg commercial price</div></div>' +
                '</div>' +
                '<div class="section-grid">' +
                    '<div class="panel">' +
                        '<div class="panel-header"><span class="panel-title">📦 Usage by Key</span></div>' +
                        '<div class="table-wrap">' +
                            '<table>' +
                                '<thead><tr><th>Key</th><th>Reqs</th><th>Tokens</th><th>Savings</th></tr></thead>' +
                                '<tbody>' +
                                    (data.keys.length === 0 ? '<tr><td colspan="4" style="color:var(--text-muted);text-align:center;padding:40px">No data</td></tr>' :
                                      data.keys.map(k => '<tr><td><span class="key-id">' + k.key_id + '</span></td><td>' + fmt(k.requests) + '</td><td>' + fmt(k.total_tokens) + '</td><td><span class="cost-tag">' + money(k.cost_saved) + '</span></td></tr>').join('')) +
                                '</tbody>' +
                            '</table>' +
                        '</div>' +
                    '</div>' +
                    '<div class="panel">' +
                        '<div class="panel-header"><span class="panel-title">🤖 Usage by Model</span></div>' +
                        '<div class="table-wrap">' +
                            '<table>' +
                                '<thead><tr><th>Model</th><th>Reqs</th><th>Tokens</th><th>Savings</th></tr></thead>' +
                                '<tbody>' +
                                    (data.models.length === 0 ? '<tr><td colspan="4" style="color:var(--text-muted);text-align:center;padding:40px">No data</td></tr>' :
                                      data.models.map(m => '<tr><td><strong>' + m.model + '</strong></td><td>' + fmt(m.requests) + '</td><td>' + fmt(m.total_tokens) + '</td><td><span class="cost-tag">' + money(m.cost_saved) + '</span></td></tr>').join('')) +
                                '</tbody>' +
                            '</table>' +
                        '</div>' +
                    '</div>' +
                    '<div class="panel full-width">' +
                        '<div class="panel-header"><span class="panel-title">📈 Tokens Last 24h</span></div>' +
                        '<div class="chart-container"><canvas id="timelineChart"></canvas></div>' +
                    '</div>' +
                    '<div class="panel full-width">' +
                        '<div class="panel-header"><span class="panel-title">🔐 Key Management</span></div>' +
                        '<div class="table-wrap">' +
                            '<table>' +
                                '<thead><tr><th>ID</th><th>Status</th><th>Priority</th><th>Requests</th><th>Actions</th></tr></thead>' +
                                '<tbody>' +
                                    data.key_status.map(k => {
                                        const statusLabel = k.status === 'active' ? '🟢 Active' : k.status === 'cooldown' ? '🟡 Cooldown' : '🔴 Invalid';
                                        return '<tr><td><span class="key-id"><span class="status-dot status-' + k.status + '"></span>' + k.id + '</span></td><td>' + statusLabel + '</td><td>' + k.priority + '</td><td>' + fmt(k.request_count) + '</td><td><button onclick="deleteKey(\\'' + k.id + '\\')" class="danger">Delete</button></td></tr>';
                                    }).join('') +
                                '</tbody>' +
                            '</table>' +
                        '</div>' +
                    '</div>' +
                    '<div class="panel full-width" id="settingsPanel">' +
                        '<div class="panel-header"><span class="panel-title">⚙️ Rotation & Anti-Detection</span></div>' +
                        '<div id="settingsContent" style="display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; padding-top: 8px;"></div>' +
                    '</div>' +
                    '<div class="panel full-width">' +
                        '<div class="panel-header"><span class="panel-title">📜 Recent Activity (Last 24h)</span></div>' +
                        '<div class="table-wrap">' +
                            '<table>' +
                                '<thead><tr><th>Time</th><th>Model</th><th>Key</th><th>Tokens (In/Out)</th><th>Status</th></tr></thead>' +
                                '<tbody>' +
                                    data.recent_activity.map(a => '<tr><td class="timestamp">' + timeAgo(a.timestamp) + '</td><td><strong>' + a.model + '</strong></td><td><span class="key-id">' + a.key_id + '</span></td><td>' + a.input_tokens + ' / ' + a.output_tokens + '</td><td><span class="status-badge status-' + a.status_code + '">' + a.status_code + '</span></td></tr>').join('') +
                                '</tbody>' +
                            '</table>' +
                        '</div>' +
                    '</div>' +
                '</div>';

            const ctx = document.getElementById('timelineChart');
            if (ctx) {
                if (timelineChart) timelineChart.destroy();
                timelineChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: data.timeline.map(t => t.hour.split(' ')[1] || t.hour),
                        datasets: [{
                            label: 'Tokens', data: data.timeline.map(t => t.tokens),
                            backgroundColor: 'rgba(139, 92, 246, 0.5)', borderRadius: 6,
                        }, {
                            label: 'Reqs', data: data.timeline.map(t => t.requests),
                            backgroundColor: 'rgba(59, 130, 246, 0.4)', borderRadius: 6, yAxisID: 'y1',
                        }]
                    },
                    options: {
                        responsive: true, maintainAspectRatio: false,
                        plugins: { legend: { labels: { color: '#8888a0' } } },
                        scales: {
                            x: { grid: { color: 'rgba(255,255,255,0.03)' }, ticks: { color: '#555570' } },
                            y: { position: 'left', grid: { color: 'rgba(255,255,255,0.03)' }, ticks: { color: '#8b5cf6' } },
                            y1: { position: 'right', display: false }
                        }
                    }
                });
            }
            renderSettings(currentConfig);
        }

        async function loadConfig() {
            try {
                const res = await fetch('/api/config');
                currentConfig = await res.json();
                renderSettings(currentConfig);
            } catch(e) { console.error('Error loading config:', e); }
        }

        function renderSettings(cfg) {
            const el = document.getElementById('settingsContent');
            if (!el) return;
            const modeLabels = {'failover':'🛡️ Failover (Recommended)','round-robin':'🔄 Round-Robin','session-sticky':'📌 Session-Sticky'};
            el.innerHTML = 
                '<div class="input-group">' +
                    '<label>Rotation Mode</label>' +
                    '<select id="cfgMode" style="background:rgba(0,0,0,0.2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 14px;color:white;font-size:14px;">' +
                        Object.entries(modeLabels).map(([v,l]) => '<option value="' + v + '" ' + (cfg.rotation_mode===v?'selected':'') + '>' + l + '</option>').join('') +
                    '</select>' +
                    '<span style="font-size:11px;color:var(--text-muted);margin-top:4px;">Failover: use 1 key until fail. Round-Robin: rotate every N reqs. Session-Sticky: same key per model.</span>' +
                '</div>' +
                '<div class="input-group" id="rrGroup" style="' + (cfg.rotation_mode==='round-robin'?'':'opacity:0.4;pointer-events:none;') + '">' +
                    '<label>Rotate every N reqs (Round-Robin)</label>' +
                    '<input type="number" id="cfgRotN" value="' + cfg.rotation_every_n + '" min="1" max="100">' +
                '</div>' +
                '<div class="input-group">' +
                    '<label>Rate Limit per key (reqs/min, 0=no limit)</label>' +
                    '<input type="number" id="cfgRate" value="' + cfg.rate_limit_per_minute + '" min="0" max="120">' +
                '</div>' +
                '<div class="input-group">' +
                    '<label>Anti-Detection Jitter</label>' +
                    '<div style="display:flex;align-items:center;gap:12px;">' +
                        '<label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px;">' +
                            '<input type="checkbox" id="cfgJitter" ' + (cfg.jitter_enabled?'checked':'') + ' style="width:16px;height:16px;"> Enabled' +
                        '</label>' +
                        '<input type="number" id="cfgJMin" value="' + cfg.jitter_min_ms + '" min="0" max="5000" style="width:80px;"> ms ~ ' +
                        '<input type="number" id="cfgJMax" value="' + cfg.jitter_max_ms + '" min="100" max="10000" style="width:80px;"> ms' +
                    '</div>' +
                    '<span style="font-size:11px;color:var(--text-muted);margin-top:4px;">Random delay before each req. Makes traffic look human.</span>' +
                '</div>' +
                '<div class="input-group" id="ssGroup" style="' + (cfg.rotation_mode==='session-sticky'?'':'opacity:0.4;pointer-events:none;') + '">' +
                    '<label>Session Sticky Window (min)</label>' +
                    '<input type="number" id="cfgSticky" value="' + cfg.session_sticky_minutes + '" min="1" max="60">' +
                '</div>' +
                '<div style="display:flex;align-items:flex-end;">' +
                    '<button onclick="saveConfig()" style="height:fit-content;">💾 Save Settings</button>' +
                '</div>';
            document.getElementById('cfgMode').onchange = (e) => {
                document.getElementById('rrGroup').style.opacity = e.target.value==='round-robin'?'1':'0.4';
                document.getElementById('rrGroup').style.pointerEvents = e.target.value==='round-robin'?'auto':'none';
                document.getElementById('ssGroup').style.opacity = e.target.value==='session-sticky'?'1':'0.4';
                document.getElementById('ssGroup').style.pointerEvents = e.target.value==='session-sticky'?'auto':'none';
            };
        }

        async function saveConfig() {
            const body = {
                rotation_mode: document.getElementById('cfgMode').value,
                rotation_every_n: parseInt(document.getElementById('cfgRotN').value),
                rate_limit_per_minute: parseInt(document.getElementById('cfgRate').value),
                jitter_enabled: document.getElementById('cfgJitter').checked,
                jitter_min_ms: parseInt(document.getElementById('cfgJMin').value),
                jitter_max_ms: parseInt(document.getElementById('cfgJMax').value),
                session_sticky_minutes: parseInt(document.getElementById('cfgSticky').value),
            };
            try {
                const res = await fetch('/api/config', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
                if (res.ok) {
                    toast('Settings saved successfully!', 'var(--accent-green)');
                    currentConfig = body;
                } else {
                    toast('Error saving settings', 'var(--accent-rose)');
                }
            } catch(e) { toast('Connection error', 'var(--accent-rose'); }
        }

        loadData();
        loadConfig();
        setInterval(loadData, 15000);
    </script>
</body>
</html>"""