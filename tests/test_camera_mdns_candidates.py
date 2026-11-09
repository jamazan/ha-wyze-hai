"""Tests for mDNS (".local") ICE candidate resolution in the Wyze camera relay."""

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.wyzeapi import camera as camera_module
from custom_components.wyzeapi.camera import (
    _async_resolve_mdns_candidates,
    _pick_lan_address,
)

NAME = "6f1c2b1e-9a3d-4e57-8c11-0b7d5f2a9e44.local"
CANDIDATE = f"candidate:1234 1 udp 2113937151 {NAME} 54321 typ host generation 0"


def _hass(infos=None, exc=None):
    getaddrinfo = AsyncMock(return_value=infos, side_effect=exc)
    return SimpleNamespace(loop=SimpleNamespace(getaddrinfo=getaddrinfo)), getaddrinfo


def _info(addr, family=socket.AF_INET):
    return (family, socket.SOCK_DGRAM, 17, "", (addr, 0))


@pytest.fixture(autouse=True)
def _clear_cache():
    camera_module._MDNS_CACHE.clear()
    yield
    camera_module._MDNS_CACHE.clear()


@pytest.mark.asyncio
async def test_trickled_candidate_is_rewritten():
    hass, _ = _hass([_info("10.0.0.16")])
    result = await _async_resolve_mdns_candidates(hass, CANDIDATE)
    assert result == CANDIDATE.replace(NAME, "10.0.0.16")


@pytest.mark.asyncio
async def test_sdp_candidates_rewritten_and_other_lines_untouched():
    sdp = (
        "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\n"
        f"a=candidate:1 1 udp 2113937151 {NAME} 50000 typ host\r\n"
        "a=candidate:2 1 udp 1677729535 203.0.113.5 50001 typ srflx raddr 0.0.0.0 rport 0\r\n"
    )
    hass, getaddrinfo = _hass([_info("10.0.0.16")])
    result = await _async_resolve_mdns_candidates(hass, sdp)
    assert "10.0.0.16 50000 typ host" in result
    assert NAME not in result
    assert "203.0.113.5 50001 typ srflx" in result
    getaddrinfo.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_mdns_candidate_never_touches_resolver():
    plain = "candidate:1 1 udp 2113937151 192.168.1.20 50000 typ host"
    hass, getaddrinfo = _hass([_info("10.0.0.16")])
    assert await _async_resolve_mdns_candidates(hass, plain) == plain
    getaddrinfo.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [OSError("no such host"), TimeoutError()])
async def test_unresolvable_name_is_left_unchanged(exc):
    hass, _ = _hass(exc=exc)
    assert await _async_resolve_mdns_candidates(hass, CANDIDATE) == CANDIDATE


@pytest.mark.asyncio
async def test_result_is_cached():
    hass, getaddrinfo = _hass([_info("10.0.0.16")])
    await _async_resolve_mdns_candidates(hass, CANDIDATE)
    await _async_resolve_mdns_candidates(hass, CANDIDATE)
    assert getaddrinfo.await_count == 1


def test_pick_lan_address_prefers_ipv4_and_skips_link_local():
    assert (
        _pick_lan_address(["fe80::1%en0", "2001:db8::5", "169.254.1.1", "10.0.0.16"])
        == "10.0.0.16"
    )
    assert _pick_lan_address(["fe80::1%en0", "2001:db8::5"]) == "2001:db8::5"
    assert _pick_lan_address(["fe80::1%en0", "127.0.0.1"]) is None


@pytest.mark.asyncio
async def test_send_candidate_and_offer_relay_resolved_address():
    import base64
    import json

    from webrtc_models import RTCIceCandidateInit

    hass, _ = _hass([_info("10.0.0.16")])
    camera = SimpleNamespace(hass=hass, name="Backyard", model="HL_PAN3")
    session = camera_module.WyzeCameraWebRTCSession("s1", camera, lambda m: None, {})
    session.websocket = AsyncMock()
    session._connected.set()

    await session.send_candidate(
        RTCIceCandidateInit(candidate=CANDIDATE, sdp_mid="0", sdp_m_line_index=0)
    )
    await session.send_offer(f"v=0\r\na={CANDIDATE}\r\n")

    def payloads():
        for call in session.websocket.send.await_args_list:
            outer = json.loads(call.args[0])
            yield json.loads(base64.b64decode(outer["messagePayload"]))

    cand_payload, offer_payload = payloads()
    assert "10.0.0.16 54321 typ host" in cand_payload["candidate"]
    assert NAME not in cand_payload["candidate"]
    assert "10.0.0.16 54321 typ host" in offer_payload["sdp"]
    assert NAME not in offer_payload["sdp"]
