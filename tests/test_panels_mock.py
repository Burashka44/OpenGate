"""
Лёгкие mock-тесты panel clients (без реальной сети).

Запуск: python -m pytest tests/test_panels_mock.py -q
или: python tests/test_panels_mock.py
"""
from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.services.panels.base import VPNAPIError
from bot.services.vpn_api import get_client_from_server_data, KNOWN_PANEL_TYPES


def test_factory_default_xui():
    from bot.services.panels.xui import XUIClient
    c = get_client_from_server_data({
        'id': 9001, 'name': 't', 'host': '127.0.0.1', 'port': 2053,
        'login': 'a', 'password': 'b', 'web_base_path': '/',
        'panel_type': '',
    })
    assert isinstance(c, XUIClient)


def test_factory_unknown_raises():
    try:
        get_client_from_server_data({
            'id': 9002, 'host': '127.0.0.1', 'port': 1,
            'login': 'a', 'password': 'b', 'panel_type': 'remnawave',
        })
        assert False, 'expected VPNAPIError'
    except VPNAPIError:
        pass


def test_marzban_supports_no_inbound():
    from bot.services.panels.marzban import MarzbanClient
    c = MarzbanClient({
        'id': 9003, 'host': '127.0.0.1', 'port': 443,
        'login': 'admin', 'password': 'x', 'protocol': 'https',
        'panel_type': 'marzban',
    })
    assert c.supports_inbound_select is False


def test_naive_link_builder():
    from bot.services.panels.naive import NaiveClient
    c = NaiveClient({
        'id': 9004, 'host': 'n.example', 'public_host': 'n.example',
        'panel_type': 'naive', 'extra_config': '{}',
    })
    link = c.build_naive_link('u1', 'p1')
    assert link.startswith('naive+https://u1:p1@n.example')


def test_mieru_merge_never_wipes_helper():
    from bot.services.panels.mieru import MieruClient
    c = MieruClient({
        'id': 9005, 'host': 'm.example', 'public_host': 'm.example',
        'panel_type': 'mieru', 'extra_config': '{}',
    })
    assert 'mieru://' in c.build_mieru_link('alice', 'secret')


def test_known_types():
    assert KNOWN_PANEL_TYPES == frozenset({'xui', 'marzban', 'naive', 'mieru'})


if __name__ == '__main__':
    test_factory_default_xui()
    test_factory_unknown_raises()
    test_marzban_supports_no_inbound()
    test_naive_link_builder()
    test_mieru_merge_never_wipes_helper()
    test_known_types()
    print('OK: panel mocks')
