"""
Лёгкие mock-тесты panel clients (без реальной сети).

Запуск: python -m pytest tests/test_panels_mock.py -q
или: python tests/test_panels_mock.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.services.panels.base import VPNAPIError
from bot.services.vpn_api import get_client_from_server_data, KNOWN_PANEL_TYPES, XUI_FAMILY


def test_factory_default_xui():
    from bot.services.panels.xui import XUIClient
    from bot.services import vpn_api as va
    va._clients.clear()
    c = get_client_from_server_data({
        'id': 9001, 'name': 't', 'host': '127.0.0.1', 'port': 2053,
        'login': 'a', 'password': 'b', 'web_base_path': '/',
        'panel_type': '',
    })
    assert isinstance(c, XUIClient)


def test_factory_lucx():
    from bot.services.panels.xui import XUIClient
    from bot.services import vpn_api as va
    va._clients.clear()
    c = get_client_from_server_data({
        'id': 9003, 'name': 'lucx', 'host': '127.0.0.1', 'port': 2053,
        'login': 'a', 'password': 'b', 'web_base_path': '/',
        'panel_type': 'lucx',
    })
    assert isinstance(c, XUIClient)
    assert hasattr(c, 'ensure_lucx_client')


def test_factory_unknown_raises():
    from bot.services import vpn_api as va
    va._clients.clear()
    try:
        get_client_from_server_data({
            'id': 9002, 'host': '127.0.0.1', 'port': 1,
            'login': 'a', 'password': 'b', 'panel_type': 'mieru_panel',
        })
        assert False, 'expected VPNAPIError'
    except VPNAPIError:
        pass


def test_known_types():
    assert KNOWN_PANEL_TYPES == frozenset({'xui', 'lucx'})
    assert XUI_FAMILY == frozenset({'xui', 'lucx'})


if __name__ == '__main__':
    test_factory_default_xui()
    test_factory_lucx()
    test_factory_unknown_raises()
    test_known_types()
    print('OK: panel mocks')
