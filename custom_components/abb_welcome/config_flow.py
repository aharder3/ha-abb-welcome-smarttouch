"""Config flow for ABB Welcome integration."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from ipaddress import IPv4Address, ip_address, ip_network
import logging
import re
import socket

import voluptuous as vol

from homeassistant.components import network
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ABB_PASSWORD,
    CONF_ABB_USERNAME,
    CONF_ALLOW_PICKUP,
    CONF_GATEWAY_IP,
    CONF_GATEWAY_UUID_OVERRIDE,
    CONF_LAN_RTSP_HOST,
    CONF_LAN_RTSP_PORT,
    CONF_UNLOCK_STRATEGY,
    DEFAULT_ALLOW_PICKUP,
    DEFAULT_LAN_RTSP_PORT,
    DEFAULT_UNLOCK_STRATEGY,
    DOMAIN,
    GATEWAY_CLIENT_TYPE,
    SMARTTOUCH_CLIENT_TYPE,
    SIP_PORT_TLS,
    UNLOCK_STRATEGIES,
)
from .portal import (
    GatewayAdminError,
    PortalError,
    compute_integrity_code,
    default_client_name,
    derive_identity,
    discover_welcome_device,
    gateway_authorize,
    gateway_local_info,
    generate_keypair_and_csr,
    parse_acl_update,
    poll_acl_update,
    request_certificate,
    resolve_portal_url,
    send_connect_event,
)

_LOGGER = logging.getLogger(__name__)
_LOG_PREFIX = "[abb] "


def _log_info(msg: str, *args: object) -> None:
    _LOGGER.info(_LOG_PREFIX + msg, *args)


def _log_error(msg: str, *args: object) -> None:
    _LOGGER.error(_LOG_PREFIX + msg, *args)


CONF_GATEWAY_PASSWORD = "gateway_password"  # noqa: S105 (config-flow field name)

POLL_ATTEMPTS = 60
POLL_ATTEMPTS_MANUAL = 100  # ~5 minutes — gives user time to click through the gateway UI
POLL_INTERVAL = 3.0

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

DISCOVERY_MANUAL = "__manual__"
DISCOVERY_TIMEOUT = 0.35
DISCOVERY_CONCURRENCY = 48
DISCOVERY_MAX_HOSTS_PER_ADAPTER = 254

GATEWAY_WEB_PORT = 443


def _port_reachable(host: str, port: int, timeout: float = 5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _gateway_web_reachable(host: str, timeout: float = 5) -> bool:
    """Return whether the classic gateway web admin is reachable."""
    return _port_reachable(host, GATEWAY_WEB_PORT, timeout)


def _welcome_device_reachable(host: str, timeout: float = 5) -> bool:
    """Accept either classic HTTPS admin or local SIP-TLS.

    SmartTouch 10 exposes SIP-TLS on 5061 but no classic gateway web admin.
    """
    return _gateway_web_reachable(host, timeout) or _port_reachable(
        host, SIP_PORT_TLS, timeout
    )


async def _async_port_reachable(
    host: str, port: int, timeout: float = DISCOVERY_TIMEOUT
) -> bool:
    """Check one TCP port without blocking Home Assistant's event loop."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (TimeoutError, OSError):
        return False

    writer.close()
    with suppress(Exception):
        await writer.wait_closed()

    return True


async def _async_probe_welcome_host(
    host: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, str] | None:
    """Return an ABB Welcome candidate when a known local port answers."""
    async with semaphore:
        https_open, sip_tls_open = await asyncio.gather(
            _async_port_reachable(host, GATEWAY_WEB_PORT),
            _async_port_reachable(host, SIP_PORT_TLS),
        )

    if not https_open and not sip_tls_open:
        return None

    if sip_tls_open and not https_open:
        device_type = "smarttouch"
        label = f"SmartTouch 10 candidate — {host}"
    elif https_open:
        device_type = "gateway"
        label = f"ABB Welcome IP gateway candidate — {host}"
    else:
        device_type = "unknown"
        label = f"ABB Welcome candidate — {host}"

    return {
        "host": host,
        "device_type": device_type,
        "label": label,
    }


async def _async_discover_local_devices(hass) -> list[dict[str, str]]:
    """Scan local IPv4 networks for ABB Welcome candidates.

    Large networks are restricted to the /24 containing Home Assistant.
    Manual IP entry is always available as fallback.
    """
    adapters = await network.async_get_adapters(hass)
    hosts: set[str] = set()

    for adapter in adapters:
        if adapter.get("enabled") is False:
            continue

        for ip_info in adapter.get("ipv4", []):
            local_raw = ip_info.get("address")
            prefix_raw = ip_info.get("network_prefix")

            if not local_raw or prefix_raw is None:
                continue

            try:
                local_ip = ip_address(local_raw)

                if not isinstance(local_ip, IPv4Address):
                    continue

                if local_ip.is_loopback or local_ip.is_link_local:
                    continue

                prefix = int(prefix_raw)

                # Never scan more than one /24 per adapter.
                scan_prefix = max(prefix, 24)

                subnet = ip_network(
                    f"{local_ip}/{scan_prefix}",
                    strict=False,
                )

            except (ValueError, TypeError):
                continue

            candidates = list(subnet.hosts())

            if len(candidates) > DISCOVERY_MAX_HOSTS_PER_ADAPTER:
                candidates = candidates[:DISCOVERY_MAX_HOSTS_PER_ADAPTER]

            for candidate in candidates:
                if candidate != local_ip:
                    hosts.add(str(candidate))

    if not hosts:
        return []

    semaphore = asyncio.Semaphore(DISCOVERY_CONCURRENCY)

    results = await asyncio.gather(
        *(
            _async_probe_welcome_host(host, semaphore)
            for host in sorted(
                hosts,
                key=lambda value: int(ip_address(value)),
            )
        )
    )

    return [
        result
        for result in results
        if result is not None
    ]


class ABBWelcomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for ABB Welcome."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "ABBWelcomeOptionsFlow":
        return ABBWelcomeOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._username = ""
        self._password = ""
        self._gateway_ip = ""
        self._gateway_password = ""
        self._portal_url = ""
        self._private_key_pem = b""
        self._cert_pem = b""
        self._client_name = ""
        self._sip_username = ""
        self._sip_password = ""
        self._sip_domain = ""
        self._own_uuid = ""
        self._gateway_uuid = ""
        self._gateway_uuid_override = ""
        self._gateway_name = ""
        self._welcome_client_type = ""
        self._gateway_sid = ""
        self._fingerprint = ""
        self._integrity_eight = ""
        self._integrity_display = ""
        self._doors: list[dict] = []
        self._manual_authorize = False
        self._authorize_error_msg = ""
        self._device_type = ""
        self._discovered_devices: list[dict[str, str]] = []

    async def _check_unique(self, gateway_uuid: str) -> ConfigFlowResult | None:
        await self.async_set_unique_id(gateway_uuid)
        self._abort_if_unique_id_configured()
        for entry in self._async_current_entries():
            if entry.data.get(CONF_GATEWAY_IP) == self._gateway_ip:
                return self.async_abort(reason="already_configured")
        return None

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Start setup by scanning the local network."""
        return await self.async_step_scan()

    async def async_step_scan(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Discover SmartTouch 10 panels and ABB Welcome IP gateways."""
        self._discovered_devices = await _async_discover_local_devices(
            self.hass
        )

        if self._discovered_devices:
            return await self.async_step_select_device()

        return await self.async_step_manual()

    async def async_step_select_device(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Select one discovered ABB Welcome device."""
        if user_input is not None:
            selected = str(user_input["device"])

            if selected == DISCOVERY_MANUAL:
                return await self.async_step_manual()

            match = next(
                (
                    device
                    for device in self._discovered_devices
                    if device["host"] == selected
                ),
                None,
            )

            if match is None:
                return await self.async_step_scan()

            self._gateway_ip = match["host"]
            self._device_type = match["device_type"]

            return await self.async_step_login()

        options = [
            {
                "value": device["host"],
                "label": device["label"],
            }
            for device in self._discovered_devices
        ]

        options.append(
            {
                "value": DISCOVERY_MANUAL,
                "label": "Enter IP address manually",
            }
        )

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            description_placeholders={
                "device_count": str(len(self._discovered_devices)),
            },
        )

    async def async_step_manual(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Accept a manually entered IPv4 address."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = str(
                user_input[CONF_GATEWAY_IP]
            ).strip()

            try:
                parsed = ip_address(host)

                if not isinstance(parsed, IPv4Address):
                    raise ValueError

            except ValueError:
                errors[CONF_GATEWAY_IP] = "invalid_ip"

            else:
                reachable = await self.hass.async_add_executor_job(
                    _welcome_device_reachable,
                    host,
                )

                if not reachable:
                    errors["base"] = "cannot_connect"

                else:
                    self._gateway_ip = str(parsed)

                    web_reachable = (
                        await self.hass.async_add_executor_job(
                            _gateway_web_reachable,
                            self._gateway_ip,
                        )
                    )

                    self._device_type = (
                        "gateway"
                        if web_reachable
                        else "smarttouch"
                    )

                    return await self.async_step_login()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GATEWAY_IP,
                        description={
                            "suggested_value": "192.168.1.100"
                        },
                    ): selector.TextSelector()
                }
            ),
            errors=errors,
        )

    async def async_step_login(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Collect credentials after local device selection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = str(
                user_input[CONF_ABB_USERNAME]
            ).strip()

            self._password = user_input[CONF_ABB_PASSWORD]

            self._gateway_password = str(
                user_input.get(CONF_GATEWAY_PASSWORD, "")
            )

            uuid_raw = str(
                user_input.get(
                    CONF_GATEWAY_UUID_OVERRIDE,
                    "",
                )
            ).strip().lower()

            if uuid_raw and not _UUID_RE.fullmatch(uuid_raw):
                errors[CONF_GATEWAY_UUID_OVERRIDE] = "invalid_uuid"

            self._gateway_uuid_override = uuid_raw

            if not errors:
                reachable = await self.hass.async_add_executor_job(
                    _welcome_device_reachable,
                    self._gateway_ip,
                )

                if not reachable:
                    errors["base"] = "cannot_connect"

            if not errors:
                try:
                    await self.hass.async_add_executor_job(
                        self._do_pairing_setup
                    )

                except GatewayAdminError as err:
                    msg = str(err).lower()

                    if "login failed" in msg:
                        errors["base"] = "gateway_admin_auth_failed"

                    elif "missing uuid" in msg or "op=6" in msg:
                        errors["base"] = "gateway_op6_failed"

                    else:
                        _log_error(
                            "Gateway admin error during setup: %s",
                            err,
                        )
                        errors["base"] = "gateway_admin_failed"

                except PortalError as err:
                    msg = str(err).lower()

                    if "401" in msg or "auth" in msg:
                        errors["base"] = "invalid_auth"

                    elif (
                        "no discovery" in msg
                        or "gateway entry" in msg
                    ):
                        errors["base"] = "gateway_not_found"

                    else:
                        _log_error(
                            "Portal pairing error: %s",
                            err,
                        )
                        errors["base"] = "unknown"

                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception(
                        "Unexpected portal setup error: %s",
                        err,
                    )
                    errors["base"] = "unknown"

                else:
                    if (
                        self._welcome_client_type
                        == SMARTTOUCH_CLIENT_TYPE
                    ):
                        self._manual_authorize = True

                        self._authorize_error_msg = (
                            "SmartTouch pairing uses the "
                            "panel/MyBuildings flow; no classic "
                            "gateway web admin is available."
                        )

                        return await self.async_step_manual_authorize()

                    try:
                        await self.hass.async_add_executor_job(
                            self._do_gateway_authorize
                        )

                    except GatewayAdminError as err:
                        msg = str(err).lower()

                        if "login failed" in msg:
                            errors["base"] = (
                                "gateway_admin_auth_failed"
                            )

                        else:
                            _log_info(
                                "Auto-approve failed (%s); "
                                "offering manual approval",
                                err,
                            )

                            self._manual_authorize = True
                            self._authorize_error_msg = str(err)

                            return await (
                                self.async_step_manual_authorize()
                            )

                    except Exception as err:  # noqa: BLE001
                        _LOGGER.exception(
                            "Unexpected error during "
                            "auto-approve: %s",
                            err,
                        )
                        errors["base"] = "unknown"

                    else:
                        return await self.async_step_poll_acl()

        schema: dict = {
            vol.Required(
                CONF_ABB_USERNAME
            ): selector.TextSelector(),

            vol.Required(
                CONF_ABB_PASSWORD
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD
                )
            ),
        }

        # Classic IP Gateway:
        # show local admin password.
        #
        # SmartTouch 10:
        # no classic gateway web-admin -> hide this field.
        if self._device_type == "gateway":
            schema[
                vol.Required(CONF_GATEWAY_PASSWORD)
            ] = selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD
                )
            )

        schema[
            vol.Optional(
                CONF_GATEWAY_UUID_OVERRIDE,
                default="",
            )
        ] = selector.TextSelector()

        device_label = (
            "SmartTouch 10"
            if self._device_type == "smarttouch"
            else "ABB Welcome IP gateway"
        )

        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "gateway_ip": self._gateway_ip,
                "device_type": device_label,
            },
        )

    def _do_pairing_setup(self) -> None:
        """Synchronous helper for the portal-side setup steps."""
        self._client_name = default_client_name()
        priv_pem, csr_pem, _ = generate_keypair_and_csr(self._username)
        self._private_key_pem = priv_pem

        self._portal_url = resolve_portal_url(self._username)
        self._cert_pem = request_certificate(
            self._portal_url,
            self._username,
            self._password,
            csr_pem,
            self._client_name,
        )

        identity = derive_identity(self._cert_pem, self._username)
        self._sip_username = identity["sip_username"]
        self._fingerprint = identity["fingerprint_sha1"]
        self._own_uuid = identity["own_portal_uuid"]

        # Classic IP gateways have a local web admin and can keep the
        # original fast/local UUID lookup. SmartTouch 10 has no such web UI,
        # so select its ``welcome.panel`` identity from MyBuildings discovery.
        web_reachable = _gateway_web_reachable(self._gateway_ip)
        if web_reachable and self._gateway_password:
            if self._gateway_uuid_override:
                self._gateway_uuid = self._gateway_uuid_override
                self._gateway_name = "ABB Welcome Gateway"
            else:
                gw_info = gateway_local_info(
                    self._gateway_ip, self._gateway_password
                )
                self._gateway_uuid = gw_info["uuid"]
                self._gateway_name = (
                    gw_info.get("portalname") or "ABB Welcome Gateway"
                )
            self._welcome_client_type = GATEWAY_CLIENT_TYPE
        else:
            device = discover_welcome_device(
                self._portal_url,
                self._cert_pem,
                self._private_key_pem,
                portal_uuid=self._gateway_uuid_override,
            )
            self._gateway_uuid = device["uuid"]
            self._gateway_name = device["name"]
            self._welcome_client_type = device["client_type"]
            if (
                self._welcome_client_type == GATEWAY_CLIENT_TYPE
                and not self._gateway_password
            ):
                raise GatewayAdminError(
                    "Gateway admin password required for a classic IP gateway"
                )

        self._integrity_eight, self._integrity_display = compute_integrity_code(
            self._fingerprint
        )
        send_connect_event(
            self._portal_url,
            self._cert_pem,
            self._private_key_pem,
            self._gateway_uuid,
            self._own_uuid,
            self._integrity_eight,
        )

    def _do_gateway_authorize(self) -> None:
        """Approve our pairing on the gateway via its admin CGI."""
        self._gateway_sid = gateway_authorize(
            self._gateway_ip,
            self._gateway_password,
            self._client_name,
            self._integrity_eight,
        )

    async def async_step_manual_authorize(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Show manual-approval instructions when auto-approve fails.

        The pairing request is already on the gateway (the portal connect
        event went through), so the user can finish the approval via the
        gateway's own web UI.
        """
        if user_input is not None:
            return await self.async_step_poll_acl()

        return self.async_show_form(
            step_id="manual_authorize",
            data_schema=vol.Schema({}),
            description_placeholders={
                "gateway_ip": self._gateway_ip,
                "client_name": self._client_name,
                "integrity_code": self._integrity_display,
                "error_detail": self._authorize_error_msg or "(no details)",
            },
        )

    async def async_step_poll_acl(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Poll for the ACL-update event the gateway pushed after approval."""
        attempts = POLL_ATTEMPTS_MANUAL if self._manual_authorize else POLL_ATTEMPTS
        try:
            payload = await self.hass.async_add_executor_job(
                poll_acl_update,
                self._portal_url,
                self._cert_pem,
                self._private_key_pem,
                self._own_uuid,
                attempts,
                POLL_INTERVAL,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("ACL polling error: %s", err)
            return self.async_abort(reason="unknown")

        if not payload:
            return self.async_abort(reason="acl_timeout")

        try:
            sip_password, sip_domain, doors_meta = await self.hass.async_add_executor_job(
                parse_acl_update, payload, self._private_key_pem
            )
        except PortalError as err:
            _log_error("ACL parse failed: %s", err)
            return self.async_abort(reason="acl_parse_failed")

        self._sip_password = sip_password
        self._sip_domain = sip_domain
        self._doors = []
        for idx, d in enumerate(doors_meta):
            door = {
                "name": d["name"],
                "address": d["address"],
                "station_id": d["station_id"],
                "body": "1",
                "index": idx,
            }
            if d.get("type"):
                door["type"] = d["type"]
            if d.get("can_unlock") is False:
                door["can_unlock"] = False
            self._doors.append(door)

        abort = await self._check_unique(self._gateway_uuid)
        if abort is not None:
            return abort

        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Show discovered doors and create the entry."""
        if user_input is not None:
            return self.async_create_entry(
                title=f"ABB Welcome ({self._gateway_name})",
                data={
                    CONF_GATEWAY_IP: self._gateway_ip,
                    "sip_username": self._sip_username,
                    "sip_password": self._sip_password,
                    "sip_domain": self._sip_domain,
                    "doors": self._doors,
                    "gateway_uuid": self._gateway_uuid,
                    "welcome_client_type": self._welcome_client_type,
                    "own_portal_uuid": self._own_uuid,
                    "client_name": self._client_name,
                    "gateway_admin_password": self._gateway_password,
                    "private_key_pem": self._private_key_pem.decode(),
                    "certificate_pem": self._cert_pem.decode(),
                },
            )

        door_lines = "\n".join(
            f"- {d['name']} (sip:{d['station_id']}@{self._sip_domain})"
            for d in self._doors
        )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "door_count": str(len(self._doors)),
                "door_names": door_lines,
                "sip_username": self._sip_username,
                "sip_domain": self._sip_domain,
            },
        )


class ABBWelcomeOptionsFlow(OptionsFlow):
    """Allow the user to change the unlock strategy after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = dict(user_input)
            data[CONF_LAN_RTSP_HOST] = str(
                data.get(CONF_LAN_RTSP_HOST) or ""
            ).strip()
            data[CONF_LAN_RTSP_PORT] = int(
                data.get(CONF_LAN_RTSP_PORT) or DEFAULT_LAN_RTSP_PORT
            )
            return self.async_create_entry(title="", data=data)

        current = self._entry.options.get(
            CONF_UNLOCK_STRATEGY, DEFAULT_UNLOCK_STRATEGY
        )
        current_rtsp_host = self._entry.options.get(CONF_LAN_RTSP_HOST, "")
        current_rtsp_port = int(
            self._entry.options.get(CONF_LAN_RTSP_PORT, DEFAULT_LAN_RTSP_PORT)
            or DEFAULT_LAN_RTSP_PORT
        )
        current_allow_pickup = bool(
            self._entry.options.get(CONF_ALLOW_PICKUP, DEFAULT_ALLOW_PICKUP)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_UNLOCK_STRATEGY, default=current): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(UNLOCK_STRATEGIES),
                        translation_key=CONF_UNLOCK_STRATEGY,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_LAN_RTSP_HOST, default=current_rtsp_host
                ): selector.TextSelector(),
                vol.Required(
                    CONF_LAN_RTSP_PORT, default=current_rtsp_port
                ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
                vol.Required(
                    CONF_ALLOW_PICKUP, default=current_allow_pickup
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
