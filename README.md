# ABB Welcome - Home Assistant integration

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=aharder3&repository=ha-abb-welcome-smarttouch&category=integration)

Local controls, ring detection, and live intercom streams for ABB Welcome /
Busch-Jaeger building intercoms backed by a **classic IP gateway** or an
**ABB/Busch-Jaeger SmartTouch 10** acting as the Welcome IP/SIP bridge.

This integration is LAN-first. Pairing uses the ABB MyBuildings cloud portal
once, then unlocks, realtime ring detection, and live video/audio run directly
against the gateway on your local network.

For Apple Home / HomeKit, use the companion
[ABB HA Doorbell Scrypted plugin][scrypted-bridge]. The Scrypted plugin imports
ABB Welcome stations into Apple Home as full HomeKit doorbells with live video,
doorbell notifications, and two-way audio.

> [!IMPORTANT]
> **Want ABB Welcome in Apple Home? Use Scrypted.**
>
> HA's native HomeKit bridge can expose a basic camera/ring sensor, but it does
> not provide the full HomeKit doorbell experience. For Apple Home notifications,
> live video, audio, talkback, and safer pickup handling, install this HA
> integration first, then add the companion
> [ABB HA Doorbell Scrypted plugin][scrypted-bridge].


## SmartTouch 10 support

This fork adds generic SmartTouch 10 support without embedding any installation-
specific data. SmartTouch is discovered through the MyBuildings device type
`com.abb.ispf.client.welcome.panel`; no local web-admin password or fixed UUID is
required. If a MyBuildings account contains multiple compatible Welcome devices,
enter the desired device's Portal UUID during setup.

Verified SmartTouch media path:

- local SIP-TLS registration on TCP 5061
- Welcome outdoor station addresses from the signed ACL update
- H.264 video over RTP
- PCMA/G.711 audio over RTP
- local camera streaming works without a separate ABB 83342 IP gateway

The one-time MyBuildings pairing still uses ABB's portal to issue the client
certificate and ACL. After pairing, SIP/intercom media runs locally.

## Features

- One Home Assistant **button entity per unlock-capable outdoor station**.
- **Camera entities** for discovered door stations, backed by HA's bundled
  go2rtc/WebRTC path.
- **LAN H.264 video + PCMA/G.711 audio** for live intercom streams.
- **Talkback services** for the active stream. The Scrypted plugin uses these to
  provide HomeKit microphone audio.
- **Streaming enabled switch** to explicitly arm live streaming. Turning it off
  tears down active streams and hangs up active calls.
- **Allow pickup switch** for incoming doorbell calls. When disabled, HA will not
  accept the ringing INVITE, leaving phones and indoor stations free to answer.
- **Realtime ring binary sensor** that listens for local SIP INVITE packets and
  fires quickly when someone presses the doorbell.
- `abb_welcome_ring` Home Assistant event with station id, station name, caller
  URI, call id, and timestamp.
- **Image entity** with the latest gateway screenshot from event history.
- **Event entity** and **last-event sensor** for ring / call / door-open history.
- **Refresh Events** button for a manual portal event poll.
- **Refresh outdoor stations** service for re-reading the gateway door list.
- Switchable unlock strategy for gateways that need a different SIP unlock path.
- LAN RTSP proxy for Scrypted/HomeKit, with automatic free-port selection and
  discovery refresh events.

## Requirements

- An ABB Welcome IP endpoint reachable on your LAN: a classic **83342 / mrange**
  IP gateway **or ABB/Busch-Jaeger SmartTouch 10** linked to Welcome.
- An **ABB-Welcome / Busch-Jaeger MyBuildings** account already linked to that
  gateway.
- Classic IP gateway only: the gateway **web admin password**. SmartTouch 10
  does not require a gateway web-admin password.
- For Apple Home: a working Scrypted installation and the
  [ABB HA Doorbell Scrypted plugin][scrypted-bridge].

## Installation

### HACS

Click the badge to add this repository to HACS:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=aharder3&repository=ha-abb-welcome-smarttouch&category=integration)

Then install **ABB Welcome** from HACS and restart Home Assistant.

If the badge does not work, open HACS -> **Custom repositories**, add
`https://github.com/aharder3/ha-abb-welcome-smarttouch` as an *Integration*, and install it.

### Manual

Copy `custom_components/abb_welcome/` into Home Assistant's
`config/custom_components/` directory and restart Home Assistant.

## Home Assistant Setup

Open Settings -> **Devices & Services** -> **Add Integration** -> **ABB Welcome**.

Fill in:

- MyBuildings portal **username**
- MyBuildings portal **password**
- ABB Welcome device local **IP address**
- Classic IP gateway only: **web admin password** (leave blank for SmartTouch 10)

Optional: if automatic setup cannot read the gateway UUID from the local
`portalclient.cgi` endpoint, fill in **Gateway Portal UUID** from the gateway web
admin Portal page or ABB Welcome mobile app, then retry.

The integration then:

1. Generates a fresh RSA keypair and requests a client certificate from the
   MyBuildings portal.
2. Selects the Welcome device: classic gateways can use the local admin API; SmartTouch panels are discovered through MyBuildings (`welcome.panel`).
3. Computes the gateway integrity code from the certificate fingerprint.
4. Sends a `welcome.connect` event so the gateway sees a pending pairing entry.
5. Classic gateways are approved through the gateway admin API; SmartTouch
   completes the same Welcome pairing through the panel/MyBuildings flow.
6. Polls for the gateway ACL update, decrypts the SIP password, reads the door
   list, and creates HA entities.

A successful pairing typically completes in under 15 seconds.

## Apple Home / HomeKit

For full Apple Home / HomeKit doorbell support, use the companion
[ABB HA Doorbell Scrypted plugin](https://github.com/rankjie/abb-ha-doorbell).

This Home Assistant integration provides the ABB Welcome connection, door
stations, ring events, camera streams, SIP/RTP media, door control and talkback
services.

The companion Scrypted plugin uses these Home Assistant features to expose the
ABB Welcome door stations to Apple Home as HomeKit doorbells with live video,
doorbell notifications and two-way audio.

Home Assistant's native HomeKit bridge can be used for basic camera exposure,
but the companion Scrypted plugin is required for the full doorbell and
two-way-audio experience.

For Scrypted installation, Apple Home setup, HomeKit configuration and
troubleshooting, see:

**[ABB HA Doorbell for Scrypted →](https://github.com/rankjie/abb-ha-doorbell)**

## Entities

The integration creates one HA device for the gateway.

For each unlock-capable outdoor station:

- `button.<gateway>_<door_name>` - unlocks that station.
- `camera.<gateway>_<door_name>` - live intercom stream for that station.

Gateway-level entities:

- `switch.<gateway>_streaming_enabled` - arms stream startup for a short window.
  Turning it off tears down active streams/calls.
- `switch.<gateway>_allow_pickup` - allows or refuses incoming-call pickup.
- `binary_sensor.<gateway>_intercom_ringing` - turns on briefly when a SIP ring
  is observed.
- `image.<gateway>_latest_screenshot` - latest gateway screenshot from event
  history.
- `event.<gateway>_intercom` - ring / call / door-open event entity.
- `sensor.<gateway>_last_event` - latest non-screenshot portal event.
- `sensor.<gateway>_sip_listener` - diagnostic state for the realtime SIP
  listener.
- `button.<gateway>_refresh_events` - manually poll portal event history.

Unlock example:

```yaml
service: button.press
target:
  entity_id: button.abb_welcome_outdoor_1
```

## Streaming

ABB intercom media is exclusive, so live streams are gated.

Manual stream:

1. Turn on `switch.<gateway>_streaming_enabled`.
2. Open the desired `camera.<gateway>_<door_name>` within the armed window.
3. HA dials the selected station and passes H.264 video plus PCMA/G.711 audio to
   go2rtc/WebRTC and to Scrypted/HomeKit.

Turning off `switch.<gateway>_streaming_enabled` immediately disarms streaming
and closes active stream sessions.

The switch exposes useful attributes:

- `reason`: why streaming is armed (`manual`, `ring`, etc.).
- `target_station_id`: station id allowed during a ring-scoped arm.
- `remaining_seconds`: time left in the arm window.

You can also arm streaming from an automation:

```yaml
service: abb_welcome.arm_streaming
data:
  station_id: "100000001"
  duration: 60
```

## Talkback

HA exposes talkback as services for the currently active stream. These services
are mainly intended for the Scrypted plugin.

- `abb_welcome.talk_start`
- `abb_welcome.talk_stop`
- `abb_welcome.talk_pcm16le`
- `abb_welcome.talk_tone`

The audio format for `talk_pcm16le` is base64-encoded 8 kHz mono signed 16-bit
little-endian PCM. HA converts it to continuous PCMA/G.711 A-law RTP on the
active call's audio leg. Idle talkback sends silence continuously, and voice
frames are queued into the same RTP sequence.

Scrypted assigns a per-client `talkback_session_id` so stale clients cannot stop
or overwrite a newer microphone session.

## Scrypted RTSP Endpoint

Scrypted needs a LAN-reachable RTSP URL. HA's bundled go2rtc RTSP listener is
localhost-only, so this integration exposes it through a small LAN TCP proxy.

Port selection starts at:

```text
rtsp://<home-assistant-lan-ip>:18556/<go2rtc_stream>
```

Each camera exposes:

- `go2rtc_stream`: the stream name, such as `abb_100000001`.
- `lan_rtsp_url`: the complete URL Scrypted should use.
- `lan_rtsp_proxy_port`: the selected proxy port.
- `lan_rtsp_proxy_running`: whether the proxy is running.

On setup/reload, the integration tries the saved preferred port first. If that
port is occupied, it scans upward from `18556`, starts on the first free port,
and persists the new port in the config entry options. This avoids requiring a
hard-coded reserved port.

After camera entities load, HA fires `abb_welcome_discovery_changed` with the
current proxy host, port, running state, and change reason. The Scrypted plugin
uses that event to refresh its station list and RTSP URLs.

## Services

- `abb_welcome.refresh_doors` - re-read outdoor stations from the gateway admin
  CGI and reload the entry if the list changed.
- `abb_welcome.arm_streaming` - arm streaming for all stations or one
  `station_id`.
- `abb_welcome.talk_start` - start sending queued microphone audio on the active
  stream.
- `abb_welcome.talk_stop` - stop voice audio and return the talkback leg to
  silence.
- `abb_welcome.talk_pcm16le` - queue base64 PCM16LE microphone audio.
- `abb_welcome.talk_tone` - send a short generated tone for testing.
- `abb_welcome.export_credentials` - export stored SIP/gateway credentials to a
  JSON file for local debugging. This output contains secrets.

## Realtime Ring Event

Every incoming SIP ring fires `abb_welcome_ring` on the Home Assistant event bus.

Example payload:

```json
{
  "caller_uri": "sip:100000001@ipgw6cce7a2bb673;user=phone",
  "caller_user": "100000001",
  "station_id": "100000001",
  "station": "Outdoor 1",
  "station_name": "Outdoor 1",
  "call_id": "1293890397@192.168.178.112",
  "received_at": 1777723346.1623127
}
```

Automation example:

```yaml
condition:
  - condition: template
    value_template: "{{ trigger.event.data.station_id == '100000001' }}"
```

## Options

Open the integration's **Configure** menu to change behavior after setup.

Options:

- **Unlock strategy**
- **Advertised Home Assistant LAN host**: blank means auto-detect.
- **Preferred LAN RTSP proxy port**: tried first; HA falls back to another free
  port if it is occupied.
- **Allow pickup from streams**: default for the `Allow pickup` switch.

### Unlock Strategy

| Strategy | What it does | When to use |
|---|---|---|
| **Hybrid** *(default)* | Plain SIP `MESSAGE` for the first outdoor station, `INVITE`-then-`MESSAGE` for the rest. | Best of both worlds on most setups. |
| **Fast** | Plain SIP `MESSAGE` for every door. | Lowest latency. Some gateways do not accept a `MESSAGE` without an active call session. |
| **Standard** | `INVITE` to bring the call up, then `MESSAGE`, then `BYE`. | Most compatible. Adds roughly 1-2 seconds per unlock. |

If a door does not open with Hybrid, switch to **Standard** first. If every door
works with **Fast**, you can leave it there for the lowest-latency setup.

## Troubleshooting

- **Cannot reach the gateway web admin on HTTPS port 443**: check the gateway IP
  and that Home Assistant can route to it.
- **Invalid portal credentials**: the MyBuildings portal rejected the username or
  password.
- **Gateway admin password is wrong**: log into `https://<gateway-ip>/` manually
  as `admin` to confirm the password.
- **Could not read the gateway's portal UUID**: fill in **Gateway Portal UUID**
  manually and retry.
- **The gateway did not see our pairing request**: retry pairing; the
  portal-to-gateway link may have been briefly delayed.
- **A door does not open**: try **Standard** unlock strategy.
- **WebRTC says `wrong response on DESCRIBE`**: turn on `Streaming enabled` and
  open the camera within the armed window.
- **Camera has video but no audio**: confirm you are on a current version and
  that the stream includes the PCMA/G.711 audio track.
- **Apple Home / Scrypted issues**: see the
  [ABB HA Doorbell Scrypted plugin documentation](https://github.com/rankjie/abb-ha-doorbell)
  for HomeKit setup and troubleshooting.

## Tested Hardware

- **ABB 83342 IP Gateway**, firmware `ASM04_GW_V6.25_20250513_MP_TIDM365`,
  system type `mrange`, 3 outdoor stations.
- **ABB/Busch-Jaeger SmartTouch 10** with Welcome 2-wire: portal type
  `com.abb.ispf.client.welcome.panel`; local SIP-TLS + H.264/PCMA RTP verified.

Reports for other models and firmware versions are welcome.

## License

MIT - see [LICENSE](LICENSE).

[scrypted-bridge]: https://github.com/rankjie/abb-ha-doorbell


---

## Credits and upstream

This project is a fork / extension of
[`rankjie/ha-abb-welcome`](https://github.com/rankjie/ha-abb-welcome).

A large part of this integration originates from the upstream project and its
contributors.

In particular, the upstream project provides the foundation for:

- ABB MyBuildings communication
- certificate based client provisioning
- Welcome pairing
- SIP signalling
- SIP-TLS support
- RTP media handling
- H.264 video
- PCMA / G.711 audio
- ring / intercom handling
- door control
- Home Assistant entities
- go2rtc / WebRTC integration
- RTSP proxy functionality
- talkback support

The SmartTouch support in this fork extends that existing work rather than
replacing it.

The intention is to keep the SmartTouch implementation generic and, where
possible, suitable for contribution back to the upstream project.

Please also consider supporting and contributing to the original project:

https://github.com/rankjie/ha-abb-welcome


## ABB / Busch-Jaeger SmartTouch support

This fork adds support for ABB / Busch-Jaeger SmartTouch devices that are
exposed through ABB MyBuildings as:

```text
com.abb.ispf.client.welcome.panel
```

Classic ABB Welcome IP gateways such as the 83342 are exposed differently and
continue to use the existing upstream integration path.

The SmartTouch implementation avoids depending on the legacy
`portalclient.cgi` web administration interface, because SmartTouch panels do
not expose that interface in the same way as classic IP gateways.

Instead, provisioning uses the existing ABB MyBuildings mechanisms:

```text
MyBuildings authentication
        |
        v
ABB signed client certificate
        |
        v
Welcome device discovery
        |
        v
com.abb.ispf.client.welcome.panel
        |
        v
welcome.connect
        |
        v
SmartTouch client approval
        |
        v
welcome.acl-update
        |
        +-- SIP identity
        +-- SIP credentials
        +-- SIP domain
        +-- outdoor stations
```

After successful provisioning, the local media path is:

```text
ABB / Busch-Jaeger Welcome outdoor station
        |
        | Welcome 2-wire
        v
SmartTouch
        |
        | SIP-TLS
        | TCP 5061
        v
Home Assistant integration
        |
        +-- PCMA / G.711 audio over RTP
        |
        +-- H.264 video over RTP
        |
        v
go2rtc / WebRTC / Home Assistant
```

SmartTouch support was developed and verified using real hardware and local
protocol testing.

Successful testing included:

- MyBuildings device discovery
- discovery of a `welcome.panel`
- certificate provisioning
- `welcome.connect`
- `welcome.acl-update`
- extraction of SIP configuration
- SIP-TLS registration
- authenticated SIP REGISTER
- SIP INVITE to an outdoor station
- H.264 RTP video
- PCMA / G.711 RTP audio
- clean SIP call termination

The implementation is designed to discover installation-specific values
dynamically.

No installation-specific IP address, Portal UUID, MyBuildings username,
password, SIP password, private key, client certificate or outdoor-station
identity should be hard-coded into this repository.


## Privacy and credentials

Never publish or commit any of the following:

- MyBuildings username/password combinations
- SIP passwords
- client private keys
- client certificates containing installation-specific identities
- Home Assistant secrets
- Home Assistant backups
- pairing credentials
- ABB Portal UUIDs belonging to a private installation
- private network addresses where they identify a real installation
- packet captures from a private network
- diagnostic exports containing credentials
- QR codes used for device pairing

If you are reporting a bug, remove or redact private information before
uploading logs or diagnostics.

In particular, do not upload files such as:

```text
*_private_key.pem
*_certificate.pem
*_credentials.json
*.pcap
*.pcapng
secrets.yaml
```

unless you have carefully verified and sanitized their contents.


## Security warning

This integration communicates with building intercom and access-control
equipment.

Depending on the device and configuration, functionality may include:

- live camera access
- microphone/audio communication
- incoming door calls
- intercom communication
- door unlocking
- building access related functions

Incorrect configuration, bugs or unexpected device behaviour could therefore
have security consequences.

Do not rely on this integration as the sole mechanism for:

- physical access security
- emergency communication
- life-safety systems
- alarm systems
- fire safety systems
- critical building security

Test door-opening automations carefully.

It is strongly recommended that door unlocking requires an explicit user
action rather than being performed automatically from untrusted triggers.


## Disclaimer

This is an independent community project.

It is not affiliated with, endorsed by, sponsored by or officially supported
by:

- ABB
- Busch-Jaeger
- Home Assistant
- HACS

ABB, Busch-Jaeger, Welcome, free@home, Home Assistant, HACS and other product
or company names may be trademarks of their respective owners.

This project uses undocumented and/or partially documented interfaces.
Firmware updates, ABB MyBuildings changes, network protocol changes or device
updates may change behaviour or break functionality at any time.

Use this software entirely at your own risk.

The maintainers and contributors cannot guarantee:

- continuous operation
- compatibility with future firmware
- compatibility with every Welcome installation
- availability of ABB cloud services
- correct operation of access-control functionality
- protection against data loss
- protection against incorrect device operation
- protection against unintended door operation

Always keep an independent method of accessing and operating your building
intercom system.


## License

This fork retains the license and copyright notices of the upstream project.

The existing `LICENSE` file must remain included when distributing copies or
substantial portions of this software.

SmartTouch-specific modifications in this fork are distributed under the same
license unless explicitly stated otherwise.

See [`LICENSE`](LICENSE) for the complete license text, warranty disclaimer and
limitation of liability.


## Contributions

Contributions are welcome.

When submitting SmartTouch-related changes, please avoid installation-specific
values and make device detection generic wherever possible.

Useful contributions include:

- additional SmartTouch models
- additional firmware versions
- improved device discovery
- improved pairing flows
- SIP interoperability improvements
- media compatibility improvements
- documentation
- translations
- automated tests

If functionality can also benefit the upstream project, contributors are
encouraged to propose the relevant changes upstream as well.
