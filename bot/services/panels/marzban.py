"""
Thin aiohttp REST-клиент Marzban (user-centric, без inbound-select).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import aiohttp

from .base import BaseVPNClient, VPNAPIError

logger = logging.getLogger(__name__)


class MarzbanClient(BaseVPNClient):
    """Клиент Marzban API (JWT + /api/user)."""

    supports_inbound_select = False

    def __init__(self, server: dict):
        super().__init__(server)
        protocol = server.get('protocol') or 'https'
        host = server.get('host') or ''
        port = server.get('port') or 443
        base_path = (server.get('web_base_path') or '').rstrip('/')
        self.base_url = f"{protocol}://{host}:{port}{base_path}".rstrip('/')
        self.username = server.get('login') or ''
        self.password = server.get('password') or ''
        self._token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            from bot.services.http_utils import DEFAULT_CLIENT_TIMEOUT
            self._session = aiohttp.ClientSession(timeout=DEFAULT_CLIENT_TIMEOUT)
        return self._session

    def _headers(self) -> Dict[str, str]:
        if not self._token:
            raise VPNAPIError("Marzban: не выполнен login")
        return {"Authorization": f"Bearer {self._token}"}

    async def login(self) -> bool:
        session = await self._session_get()
        url = f"{self.base_url}/api/admin/token"
        data = {"username": self.username, "password": self.password}
        try:
            async with session.post(url, data=data) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise VPNAPIError(f"Marzban login failed: {resp.status} {text}")
                body = await resp.json()
                self._token = body.get('access_token')
                if not self._token:
                    raise VPNAPIError("Marzban login: нет access_token")
                return True
        except VPNAPIError:
            raise
        except Exception as e:
            raise VPNAPIError(f"Marzban login error: {e}") from e

    async def _ensure_auth(self) -> None:
        if not self._token:
            await self.login()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        await self._ensure_auth()
        session = await self._session_get()
        url = f"{self.base_url}{path}"
        async with session.request(method, url, headers=self._headers(), **kwargs) as resp:
            if resp.status == 401:
                await self.login()
                async with session.request(
                    method, url, headers=self._headers(), **kwargs
                ) as resp2:
                    if resp2.status >= 400:
                        raise VPNAPIError(f"Marzban {method} {path}: {resp2.status} {await resp2.text()}")
                    if resp2.status == 204:
                        return None
                    return await resp2.json()
            if resp.status >= 400:
                raise VPNAPIError(f"Marzban {method} {path}: {resp.status} {await resp.text()}")
            if resp.status == 204:
                return None
            return await resp.json()

    async def get_inbounds(self) -> List[Dict[str, Any]]:
        # User-centric: псевдо-inbound для совместимости UX
        return [{'id': 0, 'remark': 'marzban', 'protocol': 'vless'}]

    async def get_server_status(self) -> Dict[str, Any]:
        return await self.get_stats()

    async def get_stats(self) -> Dict[str, Any]:
        try:
            await self._ensure_auth()
            data = await self._request('GET', '/api/system')
            return {
                'online': True,
                'cpu': data.get('cpu') if isinstance(data, dict) else None,
                'raw': data,
            }
        except Exception as e:
            logger.warning(f"Marzban get_stats: {e}")
            return {'online': False}

    async def get_online_clients_count(self) -> int:
        stats = await self.get_stats()
        if isinstance(stats.get('raw'), dict):
            return int(stats['raw'].get('users_active') or 0)
        return 0

    def _expire_unix(self, expire_days: int) -> int:
        if expire_days <= 0:
            return 0
        return int((datetime.now(timezone.utc) + timedelta(days=expire_days)).timestamp())

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
        username = email
        data_limit = int(total_gb) * (1024 ** 3) if total_gb and total_gb < 10**12 else int(total_gb or 0)
        payload = {
            "username": username,
            "proxies": {"vless": {}, "vmess": {}, "trojan": {}, "shadowsocks": {}},
            "inbounds": {},
            "expire": self._expire_unix(expire_days),
            "data_limit": data_limit,
            "data_limit_reset_strategy": "no_reset",
            "status": "active" if enable else "disabled",
            "note": f"tg:{tg_id}" if tg_id else "",
        }
        # Try create; if exists — modify
        try:
            user = await self._request('POST', '/api/user', json=payload)
        except VPNAPIError as e:
            if '409' in str(e) or 'already' in str(e).lower():
                user = await self._request('PUT', f'/api/user/{username}', json=payload)
            else:
                raise
        sub_url = None
        if isinstance(user, dict):
            sub_url = user.get('subscription_url')
        return {
            'id': username,
            'email': username,
            'uuid': username,
            'subscription_url': sub_url,
        }

    async def get_inbound_flow(self, inbound_id: int) -> str:
        return ''

    async def get_client_stats(self, email: str) -> Optional[Dict[str, Any]]:
        try:
            user = await self._request('GET', f'/api/user/{email}')
            if not isinstance(user, dict):
                return None
            return {
                'email': email,
                'up': user.get('used_traffic', 0),
                'down': 0,
                'total': user.get('data_limit', 0),
                'expiryTime': (user.get('expire') or 0) * 1000,
                'enable': user.get('status') == 'active',
            }
        except VPNAPIError:
            return None

    async def delete_client(self, inbound_id: int, client_uuid: str) -> bool:
        try:
            await self._request('DELETE', f'/api/user/{client_uuid}')
            return True
        except VPNAPIError as e:
            logger.warning(f"Marzban delete_client: {e}")
            return False

    async def reset_client_traffic(self, inbound_id: int, email: str) -> bool:
        try:
            await self._request('POST', f'/api/user/{email}/reset')
            return True
        except VPNAPIError as e:
            logger.warning(f"Marzban reset traffic: {e}")
            return False

    async def update_client_traffic_limit(
        self, inbound_id: int, client_uuid: str, email: str, total_gb: int
    ) -> bool:
        return await self.update_client_limit(inbound_id, client_uuid, email, total_gb * (1024 ** 3) if total_gb < 10**6 else total_gb)

    async def disable_reset_for_all_clients(self) -> int:
        return 0

    async def extend_client_expiry(
        self, inbound_id: int, client_uuid: str, email: str, days: int
    ) -> bool:
        user = await self._request('GET', f'/api/user/{email}')
        cur = int(user.get('expire') or 0)
        now = int(time.time())
        base = cur if cur > now else now
        new_exp = base + days * 86400
        await self._request('PUT', f'/api/user/{email}', json={"expire": new_exp})
        return True

    async def get_client_config(self, email: str) -> Optional[Dict[str, Any]]:
        return await self.get_client_stats(email)

    async def get_subscription_link(self, sub_id: str) -> Optional[str]:
        return await self.build_subscription_url(sub_id)

    async def build_subscription_url(self, sub_id: str) -> Optional[str]:
        try:
            user = await self._request('GET', f'/api/user/{sub_id}')
            if isinstance(user, dict) and user.get('subscription_url'):
                return user['subscription_url']
        except VPNAPIError:
            pass
        # Fallback: /sub/{token} relative to base
        return f"{self.base_url}/sub/{sub_id}"

    async def get_database_backup(self) -> bytes:
        raise VPNAPIError("Marzban: backup через API не поддерживается этим клиентом")

    async def update_client_limit(
        self, inbound_id: int, client_uuid: str, email: str, total_gb_bytes: int
    ) -> bool:
        await self._request('PUT', f'/api/user/{email}', json={"data_limit": int(total_gb_bytes or 0)})
        return True

    async def update_client_full(
        self,
        inbound_id: int,
        client_uuid: str,
        email: str,
        expiry_time_ms: int,
        total_gb_bytes: int,
    ) -> bool:
        expire = 0 if not expiry_time_ms else int(expiry_time_ms / 1000)
        await self._request(
            'PUT',
            f'/api/user/{email}',
            json={
                "expire": expire,
                "data_limit": int(total_gb_bytes or 0),
                "status": "active",
            },
        )
        return True

    async def set_clients_enabled_by_email(self, email: str, enable: bool) -> int:
        status = "active" if enable else "disabled"
        await self._request('PUT', f'/api/user/{email}', json={"status": status})
        return 1

    async def delete_clients_by_email_on_server(self, email: str) -> int:
        ok = await self.delete_client(0, email)
        return 1 if ok else 0

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
