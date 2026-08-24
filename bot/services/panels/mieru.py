"""
mieru/mita: partial JSON merge + apply + reload. Never full wipe.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import string
from typing import Optional, Dict, Any, List

from .base import BaseVPNClient, VPNAPIError

logger = logging.getLogger(__name__)


def _parse_extra(server: dict) -> dict:
    raw = server.get('extra_config') or '{}'
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _gen_password(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))


class MieruClient(BaseVPNClient):
    """Управление пользователями mieru через mita apply (merge-only)."""

    supports_inbound_select = False

    def __init__(self, server: dict):
        super().__init__(server)
        self.extra = _parse_extra(server)
        self.public_host = (server.get('public_host') or server.get('host') or '').strip()
        self.config_path = self.extra.get('config_path') or '/etc/mieru/server.json'
        self.apply_cmd = self.extra.get('apply_cmd') or 'mita apply'
        self.reload_cmd = self.extra.get('reload_cmd') or 'mita reload'
        self.ssh_host = self.extra.get('ssh_host') or server.get('host')
        self.ssh_user = self.extra.get('ssh_user') or server.get('login') or 'root'
        self.ssh_password = self.extra.get('ssh_password') or server.get('password') or ''
        self._users: Dict[str, Dict[str, Any]] = {}

    async def login(self) -> bool:
        code, _, err = await self._ssh_run("true")
        if code != 0:
            raise VPNAPIError(f"mieru SSH probe failed: {err}")
        return True

    async def _ssh_run(self, cmd: str) -> tuple:
        try:
            remote = f"{self.ssh_user}@{self.ssh_host}"
            full = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", remote, cmd]
            if self.ssh_password:
                full = ["sshpass", "-p", self.ssh_password] + full
            proc = await asyncio.create_subprocess_exec(
                *full,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            return proc.returncode or 0, out.decode(errors='replace'), err.decode(errors='replace')
        except FileNotFoundError:
            raise VPNAPIError("SSH/sshpass не доступны на хосте бота")
        except Exception as e:
            raise VPNAPIError(f"SSH error: {e}") from e

    async def _load_config(self) -> dict:
        code, content, err = await self._ssh_run(f"cat {self.config_path}")
        if code != 0:
            raise VPNAPIError(f"Cannot read mieru config: {err}")
        try:
            return json.loads(content) if content.strip() else {}
        except json.JSONDecodeError as e:
            raise VPNAPIError(f"Invalid mieru JSON: {e}") from e

    async def _apply_merge_users(self, users_patch: List[dict], remove_names: Optional[List[str]] = None) -> None:
        """Merge users into config; backup; apply; reload. Never wipe other users."""
        cfg = await self._load_config()
        # Backup
        await self._ssh_run(f"cp {self.config_path} {self.config_path}.bak.$(date +%s)")

        existing = cfg.get('users') or cfg.get('Users') or []
        if not isinstance(existing, list):
            existing = []

        by_name = {}
        for u in existing:
            name = u.get('name') or u.get('Name') or u.get('username')
            if name:
                by_name[name] = u

        for name in (remove_names or []):
            by_name.pop(name, None)

        for u in users_patch:
            name = u.get('name')
            if not name:
                continue
            prev = by_name.get(name, {})
            merged = dict(prev)
            merged.update(u)
            by_name[name] = merged

        new_users = list(by_name.values())
        if 'users' in cfg or 'Users' not in cfg:
            cfg['users'] = new_users
        else:
            cfg['Users'] = new_users

        blob = json.dumps(cfg, ensure_ascii=False, indent=2)
        b64 = base64.b64encode(blob.encode()).decode()
        write_cmd = (
            f"echo {b64} | base64 -d > {self.config_path} && "
            f"{self.apply_cmd} && {self.reload_cmd}"
        )
        code, out, err = await self._ssh_run(write_cmd)
        if code != 0:
            # restore bak best-effort
            await self._ssh_run(
                f"ls -t {self.config_path}.bak.* 2>/dev/null | head -1 | "
                f"xargs -I{{}} cp {{}} {self.config_path}"
            )
            raise VPNAPIError(f"mita apply failed: {err or out}")

    def build_mieru_link(self, name: str, password: str) -> str:
        host = self.public_host or 'localhost'
        port = self.extra.get('public_port') or 443
        return f"mieru://{name}:{password}@{host}:{port}"

    async def get_inbounds(self) -> List[Dict[str, Any]]:
        return [{'id': 0, 'remark': 'mieru', 'protocol': 'mieru'}]

    async def get_server_status(self) -> Dict[str, Any]:
        return await self.get_stats()

    async def get_stats(self) -> Dict[str, Any]:
        try:
            await self.login()
            return {'online': True}
        except Exception:
            return {'online': False}

    async def get_online_clients_count(self) -> int:
        return 0

    async def add_client(
        self,
        inbound_id: int,
        email: str,
        total_gb: int = 0,
        expire_days: int = 30,
        limit_ip: int = 1,
        enable: bool = True,
        tg_id: str = '',
        flow: str = '',
        sub_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        password = _gen_password()
        quota_bytes = int(total_gb) * (1024 ** 3) if total_gb and total_gb < 10**12 else int(total_gb or 0)
        user = {
            "name": email,
            "password": password,
        }
        if quota_bytes > 0:
            user["quotas"] = [{"days": max(expire_days, 1), "megabytes": max(1, quota_bytes // (1024 ** 2))}]
        await self._apply_merge_users([user])
        self._users[email] = user
        return {
            'id': email,
            'email': email,
            'uuid': password,
            'password': password,
            'config_link': self.build_mieru_link(email, password),
        }

    async def get_inbound_flow(self, inbound_id: int) -> str:
        return ''

    async def get_client_stats(self, email: str) -> Optional[Dict[str, Any]]:
        return {'email': email, 'enable': True}

    async def delete_client(self, inbound_id: int, client_uuid: str) -> bool:
        try:
            await self._apply_merge_users([], remove_names=[client_uuid])
            self._users.pop(client_uuid, None)
            return True
        except VPNAPIError as e:
            logger.warning(f"mieru delete: {e}")
            return False

    async def reset_client_traffic(self, inbound_id: int, email: str) -> bool:
        return True

    async def update_client_traffic_limit(self, inbound_id: int, client_uuid: str, email: str, total_gb: int) -> bool:
        return True

    async def disable_reset_for_all_clients(self) -> int:
        return 0

    async def extend_client_expiry(self, inbound_id: int, client_uuid: str, email: str, days: int) -> bool:
        return True

    async def get_client_config(self, email: str) -> Optional[Dict[str, Any]]:
        u = self._users.get(email) or {}
        pwd = u.get('password') or ''
        return {'link': self.build_mieru_link(email, pwd) if pwd else None}

    async def get_subscription_link(self, sub_id: str) -> Optional[str]:
        return None

    async def build_subscription_url(self, sub_id: str) -> Optional[str]:
        return None

    async def get_database_backup(self) -> bytes:
        code, content, err = await self._ssh_run(f"cat {self.config_path}")
        if code != 0:
            raise VPNAPIError(err)
        return content.encode()

    async def update_client_limit(self, inbound_id: int, client_uuid: str, email: str, total_gb_bytes: int) -> bool:
        return True

    async def update_client_full(
        self,
        inbound_id: int,
        client_uuid: str,
        email: str,
        expiry_time_ms: int,
        total_gb_bytes: int,
    ) -> bool:
        import time
        if expiry_time_ms and expiry_time_ms < int(time.time() * 1000):
            await self._apply_merge_users([], remove_names=[email])
            return True
        if client_uuid:
            user = {"name": email, "password": client_uuid}
            if total_gb_bytes:
                user["quotas"] = [{"days": 30, "megabytes": max(1, int(total_gb_bytes) // (1024 ** 2))}]
            await self._apply_merge_users([user])
        return True

    async def set_clients_enabled_by_email(self, email: str, enable: bool) -> int:
        if not enable:
            ok = await self.delete_client(0, email)
            return 1 if ok else 0
        return 0

    async def delete_clients_by_email_on_server(self, email: str) -> int:
        ok = await self.delete_client(0, email)
        return 1 if ok else 0

    async def close(self):
        pass
