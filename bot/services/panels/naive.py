"""
NaiveProxy: Caddy Admin API (primary) + SSH users.conf fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import string
from typing import Optional, Dict, Any, List

import aiohttp

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


class NaiveClient(BaseVPNClient):
    """Управление пользователями Naive через Caddy Admin API или SSH."""

    supports_inbound_select = False

    def __init__(self, server: dict):
        super().__init__(server)
        self.extra = _parse_extra(server)
        self.public_host = (server.get('public_host') or server.get('host') or '').strip()
        self.caddy_admin_url = (self.extra.get('caddy_admin_url') or '').rstrip('/')
        self.users_conf_path = self.extra.get('users_conf_path') or '/etc/caddy/users.conf'
        self.reload_cmd = self.extra.get('reload_cmd') or 'systemctl reload caddy'
        self.ssh_host = self.extra.get('ssh_host') or server.get('host')
        self.ssh_user = self.extra.get('ssh_user') or server.get('login') or 'root'
        self.ssh_password = self.extra.get('ssh_password') or server.get('password') or ''
        self._session: Optional[aiohttp.ClientSession] = None
        self._users: Dict[str, str] = {}  # email -> password (in-memory cache)

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def login(self) -> bool:
        if self.caddy_admin_url:
            session = await self._session_get()
            try:
                async with session.get(f"{self.caddy_admin_url}/config/") as resp:
                    if resp.status >= 400:
                        raise VPNAPIError(f"Caddy Admin unreachable: {resp.status}")
                    return True
            except VPNAPIError:
                raise
            except Exception as e:
                raise VPNAPIError(f"Caddy Admin login error: {e}") from e
        # SSH mode — probe with true
        code, _, err = await self._ssh_run("true")
        if code != 0:
            raise VPNAPIError(f"SSH probe failed: {err}")
        return True

    async def _ssh_run(self, cmd: str) -> tuple:
        """Простой SSH через sshpass/ssh (если доступен)."""
        try:
            import shlex
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

    async def _upsert_user(self, email: str, password: str) -> None:
        if self.caddy_admin_url:
            # Best-effort: store mapping in Caddy vars / custom endpoint if configured
            endpoint = self.extra.get('caddy_users_endpoint') or f"{self.caddy_admin_url}/load"
            session = await self._session_get()
            payload = {"email": email, "password": password, "action": "upsert"}
            try:
                async with session.post(endpoint, json=payload) as resp:
                    if resp.status < 400:
                        self._users[email] = password
                        return
                    logger.warning(f"Caddy upsert HTTP {resp.status}, fallback SSH if configured")
            except Exception as e:
                logger.warning(f"Caddy upsert failed: {e}")
            if not self.ssh_host:
                raise VPNAPIError("Не удалось upsert naive-пользователя через Caddy Admin")

        # SSH users.conf: user:pass per line — merge, never wipe
        code, content, err = await self._ssh_run(f"cat {self.users_conf_path} 2>/dev/null || true")
        lines = [ln for ln in content.splitlines() if ln.strip() and not ln.strip().startswith('#')]
        new_lines = []
        found = False
        for ln in lines:
            user = ln.split(':', 1)[0].strip()
            if user == email:
                new_lines.append(f"{email}:{password}")
                found = True
            else:
                new_lines.append(ln)
        if not found:
            new_lines.append(f"{email}:{password}")
        blob = "\n".join(new_lines) + "\n"
        # Write via heredoc carefully
        b64 = __import__('base64').b64encode(blob.encode()).decode()
        write_cmd = (
            f"cp {self.users_conf_path} {self.users_conf_path}.bak 2>/dev/null; "
            f"echo {b64} | base64 -d > {self.users_conf_path} && {self.reload_cmd}"
        )
        code, _, err = await self._ssh_run(write_cmd)
        if code != 0:
            raise VPNAPIError(f"SSH write users.conf failed: {err}")
        self._users[email] = password

    async def _remove_user(self, email: str) -> bool:
        if self.caddy_admin_url and self.extra.get('caddy_users_endpoint'):
            session = await self._session_get()
            try:
                async with session.post(
                    self.extra['caddy_users_endpoint'],
                    json={"email": email, "action": "delete"},
                ) as resp:
                    if resp.status < 400:
                        self._users.pop(email, None)
                        return True
            except Exception:
                pass
        if not self.ssh_host:
            return False
        code, content, _ = await self._ssh_run(f"cat {self.users_conf_path} 2>/dev/null || true")
        lines = [ln for ln in content.splitlines() if ln.strip()]
        kept = [ln for ln in lines if not ln.startswith(f"{email}:")]
        blob = "\n".join(kept) + ("\n" if kept else "")
        b64 = __import__('base64').b64encode(blob.encode()).decode()
        write_cmd = (
            f"cp {self.users_conf_path} {self.users_conf_path}.bak 2>/dev/null; "
            f"echo {b64} | base64 -d > {self.users_conf_path} && {self.reload_cmd}"
        )
        code, _, err = await self._ssh_run(write_cmd)
        if code == 0:
            self._users.pop(email, None)
            return True
        logger.warning(f"naive remove failed: {err}")
        return False

    def build_naive_link(self, email: str, password: str) -> str:
        host = self.public_host or 'localhost'
        return f"naive+https://{email}:{password}@{host}"

    async def get_inbounds(self) -> List[Dict[str, Any]]:
        return [{'id': 0, 'remark': 'naive', 'protocol': 'naive'}]

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
        await self._upsert_user(email, password)
        link = self.build_naive_link(email, password)
        return {
            'id': email,
            'email': email,
            'uuid': password,
            'password': password,
            'config_link': link,
        }

    async def get_inbound_flow(self, inbound_id: int) -> str:
        return ''

    async def get_client_stats(self, email: str) -> Optional[Dict[str, Any]]:
        return {'email': email, 'enable': email in self._users or True}

    async def delete_client(self, inbound_id: int, client_uuid: str) -> bool:
        return await self._remove_user(client_uuid)

    async def reset_client_traffic(self, inbound_id: int, email: str) -> bool:
        return True  # traffic enforce в боте

    async def update_client_traffic_limit(self, inbound_id: int, client_uuid: str, email: str, total_gb: int) -> bool:
        return True

    async def disable_reset_for_all_clients(self) -> int:
        return 0

    async def extend_client_expiry(self, inbound_id: int, client_uuid: str, email: str, days: int) -> bool:
        return True  # expiry enforce в боте

    async def get_client_config(self, email: str) -> Optional[Dict[str, Any]]:
        pwd = self._users.get(email) or ''
        return {'link': self.build_naive_link(email, pwd) if pwd else None}

    async def get_subscription_link(self, sub_id: str) -> Optional[str]:
        return None

    async def build_subscription_url(self, sub_id: str) -> Optional[str]:
        return None

    async def get_database_backup(self) -> bytes:
        raise VPNAPIError("Naive: backup не поддерживается")

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
        # Если expiry в прошлом — disable/remove
        import time
        if expiry_time_ms and expiry_time_ms < int(time.time() * 1000):
            await self._remove_user(email)
            return True
        # client_uuid хранит password
        if client_uuid:
            await self._upsert_user(email, client_uuid)
        return True

    async def set_clients_enabled_by_email(self, email: str, enable: bool) -> int:
        if not enable:
            return 1 if await self._remove_user(email) else 0
        return 0

    async def delete_clients_by_email_on_server(self, email: str) -> int:
        return 1 if await self._remove_user(email) else 0

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
