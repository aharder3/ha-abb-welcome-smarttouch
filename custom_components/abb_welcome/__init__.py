"""ABB Welcome integration — LAN door unlock + cloud event history via SIP."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import requests
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_start

from .const import CONF_UNLOCK_STRATEGY, DEFAULT_UNLOCK_STRATEGY, DOMAIN, SIP_PORT_TLS
from .coordinator import ABBWelcomeCoordinator
from .sip_client import SIPClient
from .sip_listener import IncomingCall, SipListener
from .streaming_state import ARM_REASON_RING, RING_ARM_SECONDS, arm

_LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


for _name in (
    "custom_components.abb_welcome",
    "custom_components.abb_welcome.portal",
    "custom_components.abb_welcome.config_flow",
    "custom_components.abb_welcome.coordinator",
    "custom_components.abb_welcome.sip_client",
    "custom_components.abb_welcome.sip_listener",
    "custom_components.abb_welcome.button",
    "custom_components.abb_welcome.binary_sensor",
    "custom_components.abb_welcome.camera",
    "custom_components.abb_welcome.intercom_dialer",
    "custom_components.abb_welcome.media_pipeline",
    "custom_components.abb_welcome.rtsp_server",
    "custom_components.abb_welcome.streaming_state",
    "custom_components.abb_welcome.switch",
    "custom_components.abb_welcome.image",
    "custom_components.abb_welcome.event",
    "custom_components.abb_welcome.sensor",
):
    logging.getLogger(_name).setLevel(logging.INFO)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.IMAGE,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]

POLL_INTERVAL = timedelta(seconds=30)

# Bus event fired on every incoming SIP INVITE.  Carries the caller URI,
# extracted user portion (typically the outdoor station id), and call_id.
EVENT_RING = f"{DOMAIN}_ring"

# Bus event fired for every SIP frame the listener sends or receives.
# Useful for protocol investigation / debugging — subscribe in an
# automation or via the Developer Tools "Events" listener.
EVENT_SIP_FRAME = f"{DOMAIN}_sip_frame"

# Bus event fired whenever the SIP listener transitions state
# (stopped/connecting/registered/disconnected).
EVENT_LISTENER_STATE = f"{DOMAIN}_listener_state"


def _build_client(entry: ConfigEntry) -> SIPClient:
    return SIPClient(
        host=entry.data["gateway_ip"],
        username=entry.data["sip_username"],
        password=entry.data["sip_password"],
        domain=entry.data["sip_domain"],
        doors=entry.data.get("doors", []),
        unlock_strategy=entry.options.get(
            CONF_UNLOCK_STRATEGY, DEFAULT_UNLOCK_STRATEGY
        ),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ABB Welcome from a config entry."""
    # Refresh the door topology before entities are created.  This makes a
    # normal config-entry reload pick up outdoor stations added/removed via
    # the gateway admin UI, without re-running the pairing/config flow.
    await _async_refresh_doors_for_entry(hass, entry, reload_on_change=False)

    coordinator = ABBWelcomeCoordinator(hass, entry)

    entry_data: dict = {
        "sip_client": _build_client(entry),
        "coordinator": coordinator,
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    # Initial poll
    if coordinator.has_certs:
        await coordinator.async_request_refresh()

        # Schedule periodic polling
        async def _poll_events(_now=None):
            await coordinator.async_request_refresh()

        entry.async_on_unload(
            async_track_time_interval(hass, _poll_events, POLL_INTERVAL)
        )

    # Realtime SIP listener for ring detection.  Set up only when SIP
    # credentials are present (config entries from older flows may not have
    # them; in that case we silently skip the listener and the integration
    # still works for outbound unlocks).
    sip_user = entry.data.get("sip_username")
    sip_pass = entry.data.get("sip_password")
    sip_domain = entry.data.get("sip_domain")
    gw_ip = entry.data.get("gateway_ip")
    if sip_user and sip_pass and sip_domain and gw_ip:
        door_names = {
            str(door.get("station_id", "")).strip(): str(
                door.get("name") or door.get("station_id") or ""
            )
            for door in entry.data.get("doors", []) or []
            if str(door.get("station_id", "")).strip()
        }

        def _on_ring(call: IncomingCall) -> None:
            station_id = call.caller_user
            station = door_names.get(station_id, "")
            payload = {
                "caller_uri": call.caller_uri,
                "caller_user": call.caller_user,
                "station_id": station_id,
                "station": station,
                "station_name": station,
                "call_id": call.call_id,
                "received_at": call.received_at,
            }
            hass.bus.async_fire(EVENT_RING, payload)
            sensor = entry_data.get("ringing_sensor")
            if sensor is not None:
                sensor.trigger_ring(payload)
            # Auto-arm streaming so the user can answer the ring within
            # the next minute (clicking the camera or accepting a HomeKit
            # doorbell notification triggers a stream straight away).
            arm(
                hass,
                entry.entry_id,
                reason=ARM_REASON_RING,
                duration=RING_ARM_SECONDS,
            )

        def _on_frame(payload: dict) -> None:
            hass.bus.async_fire(EVENT_SIP_FRAME, payload)
            sensor = entry_data.get("listener_state_sensor")
            if sensor is not None:
                is_invite = (
                    payload.get("direction") == "in"
                    and payload.get("method") == "INVITE"
                )
                sensor.record_frame(payload.get("direction", ""), is_invite)

        def _on_state_change(new_state: str) -> None:
            hass.bus.async_fire(
                EVENT_LISTENER_STATE,
                {"state": new_state, "at": _now_iso()},
            )
            sensor = entry_data.get("listener_state_sensor")
            if sensor is not None:
                sensor.update_state(new_state)

        listener = SipListener(
            host=gw_ip,
            username=sip_user,
            password=sip_pass,
            domain=sip_domain,
            port=SIP_PORT_TLS,
            transport="tls",
            on_ring=_on_ring,
            on_frame=_on_frame,
            on_state_change=_on_state_change,
        )
        entry_data["sip_listener"] = listener

        # Defer start until HA finishes booting.  Before EVENT_HOMEASSISTANT_
        # STARTED the network stack and other integrations may not be ready,
        # which on some setups leaves the listener task starved or its first
        # connect failing in ways that show up as a stuck "stopped" state.
        # async_at_start fires immediately if HA is already running (i.e. on
        # integration reload), so the path is the same in both cases.
        @callback
        def _start_listener(_hass: HomeAssistant) -> None:
            listener.start(_hass)

        entry.async_on_unload(async_at_start(hass, _start_listener))

        async def _stop_listener(*_args) -> None:
            await listener.stop()

        entry.async_on_unload(_stop_listener)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    return True


SERVICE_EXPORT_CREDENTIALS = "export_credentials"
SERVICE_REFRESH_DOORS = "refresh_doors"
EXPORT_FIELDS = (
    "gateway_ip",
    "sip_username",
    "sip_password",
    "sip_domain",
    "doors",
    "certificate_pem",
    "private_key_pem",
    "gateway_admin_password",
    "gateway_uuid",
    "abb_username",
)

_GATEWAY_ADMIN_TIMEOUT = 8
_GATEWAY_LOGIN_OK_RESPONSES = {"1", "2"}


def _fetch_gateway_device_list(gateway_ip: str, admin_password: str) -> str:
    """Read the gateway admin device-list CGI response."""
    errors: list[str] = []
    for scheme in ("http", "https"):
        base = f"{scheme}://{gateway_ip}"
        try:
            with requests.Session() as session:
                login = session.get(
                    f"{base}/cgi-bin/checklogin.cgi",
                    params={"name": "admin", "pwd": admin_password},
                    timeout=_GATEWAY_ADMIN_TIMEOUT,
                    verify=False,
                )
                body = login.text.strip()
                if (
                    login.status_code != 200
                    or body not in _GATEWAY_LOGIN_OK_RESPONSES
                ):
                    errors.append(
                        f"{scheme}: login returned HTTP {login.status_code} "
                        f"body={body!r}"
                    )
                    continue

                response = session.get(
                    f"{base}/cgi-bin/adduser.cgi",
                    params={"type": "getdevicelist"},
                    headers={"Referer": f"{base}/config.html"},
                    timeout=_GATEWAY_ADMIN_TIMEOUT,
                    verify=False,
                )
                if response.status_code != 200:
                    errors.append(
                        f"{scheme}: getdevicelist returned HTTP "
                        f"{response.status_code}"
                    )
                    continue
                return response.text.strip()
        except requests.RequestException as err:
            errors.append(f"{scheme}: {err}")

    raise RuntimeError("; ".join(errors) or "gateway returned no device list")


def _parse_gateway_doors(raw: str, sip_domain: str) -> list[dict]:
    """Parse adduser.cgi?type=getdevicelist into entry.data['doors']."""
    doors: list[dict] = []
    for item in filter(None, raw.split(";")):
        parts = item.split("+")
        if len(parts) < 3 or not parts[0].startswith("outdoorstation_"):
            continue

        device_id = parts[1].strip()
        name = unquote(parts[2].strip())
        if not device_id or not name:
            continue

        station_id = f"10000000{device_id}"
        doors.append(
            {
                "name": name,
                "address": f"sip:{station_id}@{sip_domain}",
                "station_id": station_id,
                "body": "1",
                "index": len(doors),
            }
        )
    return doors


def _fetch_doors_from_gateway(
    gateway_ip: str, admin_password: str, sip_domain: str
) -> list[dict] | None:
    """Pull the current outdoor-station list from the gateway admin CGI."""
    try:
        raw = _fetch_gateway_device_list(gateway_ip, admin_password)
    except RuntimeError as err:
        _LOGGER.error("[abb] refresh_doors: gateway HTTP error: %s", err)
        return None

    doors = _parse_gateway_doors(raw, sip_domain)
    if not doors:
        _LOGGER.warning(
            "[abb] refresh_doors: gateway returned no outdoorstation entries "
            "(raw=%r)",
            raw,
        )
        return None
    return doors


def _doors_equal(a: list[dict], b: list[dict]) -> bool:
    """Compare door lists ignoring persisted index metadata."""
    keys = ("name", "address", "station_id", "body")
    return [tuple(door.get(key) for key in keys) for door in a] == [
        tuple(door.get(key) for key in keys) for door in b
    ]


async def _async_refresh_doors_for_entry(
    hass: HomeAssistant, entry: ConfigEntry, *, reload_on_change: bool
) -> bool:
    """Refresh one config entry's stored door topology from the gateway."""
    gateway_ip = entry.data.get("gateway_ip")
    admin_password = entry.data.get("gateway_admin_password")
    sip_domain = entry.data.get("sip_domain")
    if not (gateway_ip and admin_password and sip_domain):
        _LOGGER.warning(
            "[abb] refresh_doors: entry %s missing gateway_ip, "
            "gateway_admin_password, or sip_domain; skipping",
            entry.entry_id,
        )
        return False

    new_doors = await hass.async_add_executor_job(
        _fetch_doors_from_gateway,
        gateway_ip,
        admin_password,
        sip_domain,
    )
    if new_doors is None:
        return False

    current = entry.data.get("doors", []) or []
    if _doors_equal(current, new_doors):
        _LOGGER.info(
            "[abb] refresh_doors: entry %s already up to date (%d door(s))",
            entry.entry_id,
            len(current),
        )
        return False

    _LOGGER.warning(
        "[abb] refresh_doors: entry %s — updating doors %d -> %d",
        entry.entry_id,
        len(current),
        len(new_doors),
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "doors": new_doors}
    )
    if reload_on_change:
        await hass.config_entries.async_reload(entry.entry_id)
    return True


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-wide services. Idempotent — safe to call on every entry setup."""
    if not hass.services.has_service(DOMAIN, SERVICE_EXPORT_CREDENTIALS):

        async def _export_creds(call: ServiceCall) -> None:
            target_entry_id = call.data.get("entry_id")
            path = call.data.get("path") or "/config/abb_welcome_creds.json"

            entries = hass.data.get(DOMAIN, {})
            if not entries:
                raise ValueError("No ABB Welcome config entries are loaded")

            if target_entry_id:
                if target_entry_id not in entries:
                    raise ValueError(f"No config entry with id={target_entry_id!r}")
                entry_ids = [target_entry_id]
            else:
                entry_ids = list(entries.keys())

            payload: dict = {"exported_at": _now_iso(), "entries": []}
            for eid in entry_ids:
                entry = hass.config_entries.async_get_entry(eid)
                if entry is None:
                    continue
                data = entry.data
                payload["entries"].append(
                    {
                        "entry_id": eid,
                        "title": entry.title,
                        **{k: data.get(k) for k in EXPORT_FIELDS if k in data},
                        "options": dict(entry.options),
                    }
                )

            target = Path(path)
            await hass.async_add_executor_job(
                target.write_text, json.dumps(payload, indent=2)
            )
            _LOGGER.warning(
                "[abb] Credentials exported to %s — file contains the SIP password, "
                "private key, and gateway admin password. Treat as sensitive.",
                target,
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_EXPORT_CREDENTIALS,
            _export_creds,
            schema=vol.Schema(
                {
                    vol.Optional("entry_id"): str,
                    vol.Optional("path"): str,
                }
            ),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_DOORS):

        async def _refresh_doors(call: ServiceCall) -> None:
            target_entry_id = call.data.get("entry_id")
            entries = hass.data.get(DOMAIN, {})
            if not entries:
                raise ValueError("No ABB Welcome config entries are loaded")

            if target_entry_id:
                if target_entry_id not in entries:
                    raise ValueError(f"No config entry with id={target_entry_id!r}")
                entry_ids = [target_entry_id]
            else:
                entry_ids = list(entries.keys())

            for eid in entry_ids:
                entry = hass.config_entries.async_get_entry(eid)
                if entry is None:
                    continue
                await _async_refresh_doors_for_entry(
                    hass, entry, reload_on_change=True
                )

        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_DOORS,
            _refresh_doors,
            schema=vol.Schema({vol.Optional("entry_id"): str}),
        )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rebuild the SIP client when the user changes options."""
    hass.data[DOMAIN][entry.entry_id]["sip_client"] = _build_client(entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload ABB Welcome config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
