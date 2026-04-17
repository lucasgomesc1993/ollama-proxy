import asyncio
import aiosqlite
import json
import aiofiles
from datetime import datetime, timedelta
from typing import List, Optional
import logging
from models import AppConfig, KeyState, KeyStatus, AllKeysExhaustedError

logger = logging.getLogger("ollama-proxy")


class KeyManager:
    def __init__(self, config: AppConfig, config_path: str = "config.json", db_path: str = "state.db"):
        self.config = config
        self.config_path = config_path
        self.db_path = db_path
        self.keys: List[KeyState] = []
        self._current_index = 0
        self._lock = asyncio.Lock()
        self._conn = None
        self._rate_counter: dict = {}
        self._session_map: dict = {}
        self._round_robin_counter: int = 0
        
        self._sync_keys_from_config()

    def _sync_keys_from_config(self):
        current_ids = {k.id for k in self.keys}
        config_ids = {k.id for k in self.config.keys}
        
        self.keys = [k for k in self.keys if k.id in config_ids]
        
        for ck in self.config.keys:
            if ck.id not in current_ids:
                self.keys.append(KeyState(
                    id=ck.id,
                    api_key=ck.api_key,
                    priority=ck.priority
                ))
        
        self.keys.sort(key=lambda x: x.priority)

    async def _save_config_to_file(self):
        if self.config_path:
            async with aiofiles.open(self.config_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(self.config.model_dump(), indent=2))

    async def add_key(self, key_data: dict):
        from models import KeyConfig
        async with self._lock:
            new_key_cfg = KeyConfig(**key_data)
            
            if any(k.id == new_key_cfg.id for k in self.config.keys):
                raise ValueError(f"Key with ID {new_key_cfg.id} already exists.")
            
            self.config.keys.append(new_key_cfg)
            await self._save_config_to_file()
            
            self._sync_keys_from_config()
            
            await self.save_state_unlocked()
            logger.info(f"New key added: {new_key_cfg.id}")

    async def delete_key(self, key_id: str):
        async with self._lock:
            if not any(k.id == key_id for k in self.config.keys):
                raise ValueError(f"Key with ID {key_id} not found.")
            
            self.config.keys = [k for k in self.config.keys if k.id != key_id]
            await self._save_config_to_file()
            
            self.keys = [k for k in self.keys if k.id != key_id]
            
            db = await self._get_db()
            await db.execute("DELETE FROM key_state WHERE id = ?", (key_id,))
            await db.commit()
            if self.db_path != ":memory:":
                await db.close()
                
            logger.info(f"Key removed: {key_id}")

    async def _get_db(self):
        if self.db_path == ":memory:":
            if self._conn is None:
                self._conn = await aiosqlite.connect(self.db_path)
            return self._conn
        return await aiosqlite.connect(self.db_path)

    async def initialize(self):
        db = await self._get_db()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS key_state (
                id TEXT PRIMARY KEY,
                status TEXT,
                cooldown_until TIMESTAMP,
                request_count INTEGER,
                last_used TIMESTAMP
            )
        """)
        await db.commit()
        
        async with db.execute("SELECT id, status, cooldown_until, request_count, last_used FROM key_state") as cursor:
            async for row in cursor:
                kid, status, cooldown_until, req_count, last_used = row
                for key in self.keys:
                    if key.id == kid:
                        if status == "invalid":
                            key.status = KeyStatus.ACTIVE
                        else:
                            key.status = KeyStatus(status)
                            
                        if cooldown_until:
                            key.cooldown_until = datetime.fromisoformat(cooldown_until)
                        key.request_count = req_count
                        if last_used:
                            key.last_used = datetime.fromisoformat(last_used)
        
        if self.db_path != ":memory:":
            await db.close()

    async def save_state(self):
        async with self._lock:
            await self.save_state_unlocked()

    async def get_active_key(self, model: str = "unknown") -> KeyState:
        async with self._lock:
            active_keys = [k for k in self.keys if k.status == KeyStatus.ACTIVE]
            
            if not active_keys:
                await self._check_key_recovery_logic()
                active_keys = [k for k in self.keys if k.status == KeyStatus.ACTIVE]
                
            if not active_keys:
                cooldown_keys = [k for k in self.keys if k.status == KeyStatus.COOLDOWN and k.cooldown_until]
                if cooldown_keys:
                    soonest = min(cooldown_keys, key=lambda x: x.cooldown_until)
                    wait_time = (soonest.cooldown_until - datetime.now()).total_seconds()
                    info = f"Next key available in {int(wait_time)}s (ID: {soonest.id})"
                else:
                    info = "All keys are marked as INVALID or no keys configured."
                
                raise AllKeysExhaustedError("No API key available at this time.", info)

            if self.config.rate_limit_per_minute > 0:
                now = datetime.now()
                rate_limited = []
                for k in active_keys:
                    minute_reqs = self._rate_counter.get(k.id, [])
                    minute_reqs = [t for t in minute_reqs if (now - t).total_seconds() < 60]
                    self._rate_counter[k.id] = minute_reqs
                    if len(minute_reqs) < self.config.rate_limit_per_minute:
                        rate_limited.append(k)
                
                if rate_limited:
                    active_keys = rate_limited
                else:
                    logger.warning("All keys are rate limited. Using least loaded.")

            rotation_mode = self.config.rotation_mode
            selected_key = None

            if rotation_mode == "failover":
                selected_key = active_keys[0]

            elif rotation_mode == "session-sticky":
                now = datetime.now()
                session_key = self._session_map.get(model)
                sticky_window = timedelta(minutes=self.config.session_sticky_minutes)
                
                if session_key:
                    kid, last_time = session_key
                    matching = [k for k in active_keys if k.id == kid]
                    if matching and (now - last_time) < sticky_window:
                        selected_key = matching[0]
                
                if not selected_key:
                    selected_key = active_keys[self._current_index % len(active_keys)]
                    self._current_index += 1
                
                self._session_map[model] = (selected_key.id, now)

            else:
                cycle_count = self._round_robin_counter
                key_index = (cycle_count // max(1, self.config.rotation_every_n)) % len(active_keys)
                selected_key = active_keys[key_index]
                self._round_robin_counter += 1

            selected_key.request_count += 1
            selected_key.last_used = datetime.now()
            
            if selected_key.id not in self._rate_counter:
                self._rate_counter[selected_key.id] = []
            self._rate_counter[selected_key.id].append(datetime.now())
            
            return selected_key

    async def mark_quota_exceeded(self, key_id: str):
        async with self._lock:
            for key in self.keys:
                if key.id == key_id:
                    key.status = KeyStatus.COOLDOWN
                    key.cooldown_until = datetime.now() + timedelta(minutes=self.config.cooldown_minutes)
                    logger.warning(f"Key {key_id} exceeded quota. Cooldown until {key.cooldown_until}")
                    await self.save_state_unlocked()
                    break

    async def mark_key_error(self, key_id: str, error_code: int):
        if error_code == 429:
            await self.mark_quota_exceeded(key_id)
        elif error_code == 401:
            async with self._lock:
                for key in self.keys:
                    if key.id == key_id:
                        key.status = KeyStatus.INVALID
                        logger.error(f"Key {key_id} marked as INVALID (Error 401).")
                        await self.save_state_unlocked()
                        break

    async def check_key_recovery(self):
        async with self._lock:
            await self._check_key_recovery_logic()

    async def _check_key_recovery_logic(self):
        now = datetime.now()
        recovered = False
        for key in self.keys:
            if key.status == KeyStatus.COOLDOWN and key.cooldown_until and now >= key.cooldown_until:
                key.status = KeyStatus.ACTIVE
                key.cooldown_until = None
                logger.info(f"Key {key.id} reactivated after cooldown.")
                recovered = True
        if recovered:
            await self.save_state_unlocked()
    
    async def save_state_unlocked(self):
        db = await self._get_db()
        for key in self.keys:
            await db.execute("""
                INSERT OR REPLACE INTO key_state (id, status, cooldown_until, request_count, last_used)
                VALUES (?, ?, ?, ?, ?)
            """, (
                key.id, 
                key.status.value, 
                key.cooldown_until.isoformat() if key.cooldown_until else None,
                key.request_count,
                key.last_used.isoformat() if key.last_used else None
            ))
        await db.commit()
        if self.db_path != ":memory:":
            await db.close()

    def get_status_summary(self) -> str:
        lines = ["\n--- Key Pool Status ---"]
        for k in self.keys:
            status_info = k.status.value
            if k.status == KeyStatus.COOLDOWN:
                status_info += f" (until {k.cooldown_until.strftime('%H:%M:%S')})"
            lines.append(f"ID: {k.id:<10} | Status: {status_info:<20} | Requests: {k.request_count}")
        lines.append("-------------------------------\n")
        return "\n".join(lines)