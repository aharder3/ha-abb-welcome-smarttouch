# ABB Welcome — Home Assistant integration

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rankjie&repository=ha-abb-welcome&category=integration)

Local controls and live camera streams for ABB Welcome / Busch-Jaeger building
intercoms backed by an **IP gateway** (system type `mrange`).

This integration is LAN-first: pairing goes through the ABB MyBuildings cloud portal
once, and from then on unlocks, realtime ring detection, and live intercom streams
run directly against your gateway on the local network. Door unlocks typically
complete in well under 100 ms.

## Features

- One Home Assistant **button entity per unlock-capable outdoor station** (Outdoor 1 / Inner / Parking, etc.).
- **WebRTC camera entities** for discovered outdoor stations, backed by HA's bundled go2rtc.
- **LAN H.264 video + PCMA/G.711 audio** for live intercom streams. The integration also exposes PCMA talkback services for the currently active call; HomeKit microphone support is provided through the included Scrypted bridge.
- **Streaming enabled switch** to explicitly arm live streaming. Intercom video/audio is building-wide exclusive, so streams do not start accidentally from frontend prefetches or HomeKit probes.
- **Allow pickup switch** — when enabled, an incoming SIP INVITE briefly arms streaming so opening the camera from the notification can pick up the ringing station. When disabled, rings force streaming off so phones and indoor stations can answer safely.
- **Image entity** with the latest doorbell screenshot. The gateway only captures a frame when someone rings, so `image_last_updated` reflects the actual ring time, not a polling timestamp.
- **Realtime ring binary_sensor** — passively listens on the gateway's local SIP port and fires within tens of milliseconds of someone pressing the doorbell. Also emits an `abb_welcome_ring` event on the HA bus with caller URI, call id, station id, and configured station name for automations. Does not interfere with the indoor stations or the official ABB app.
- **Refresh Events** button — forces a portal poll if you don't want to wait for the next 30 s tick.
- **Event entity** + **last-event sensor** for ring / call / door-open history, including event ids, timestamps, sender, call grouping id, payload text, and station details when the gateway/cloud event provides them.
- LAN-only runtime for unlocks, ring detection, and live streams after pairing.
- Fully automated pairing — fill in four fields, the integration does the rest.
- Switchable unlock strategy if the default doesn't work on your gateway.

## Requirements

- An ABB Welcome **IP gateway** that you can reach on your local network
  (e.g. ABB **83342** or another `mrange`-system IP gateway, typically reachable
  at `192.168.x.x`).
- An **ABB-Welcome / Busch-Jaeger MyBuildings** account that is already linked to
  that gateway (the same login you use in the official ABB Welcome mobile app).
- The gateway's **web admin password** (the one used at `https://<gateway-ip>/`).
  Required for automated pairing — used during setup and stored with the
  config entry so re-pairing/renewal can run without interaction.

## Installation

### Via HACS (recommended)

Click the badge to add this repository to HACS in one step:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rankjie&repository=ha-abb-welcome&category=integration)

Then install **ABB Welcome** from HACS and restart Home Assistant.

If the button doesn't work (e.g. you haven't configured My Home Assistant): in HACS, open **⋮** → **Custom repositories**, add `https://github.com/rankjie/ha-abb-welcome` as an *Integration*, and install from there.

### Manual

Copy `custom_components/abb_welcome/` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

Settings → **Devices & Services** → **Add Integration** → **ABB Welcome**.

Fill in the required fields:

- MyBuildings portal **username**
- MyBuildings portal **password**
- Gateway local **IP address**
- Gateway **web admin password**

Optional: if automatic setup cannot read the gateway UUID from the local
`portalclient.cgi` endpoint, fill in **Gateway Portal UUID** from the gateway
web admin Portal page or ABB Welcome mobile app, then retry.

The integration then runs end-to-end without any further interaction:

1. Generates a fresh RSA keypair and requests a client certificate from the portal
   (HTTP Digest auth, returns 201 with a raw PEM).
2. Pulls the gateway's UUID from its local admin API.
3. Computes an 8-character **integrity code** locally from the cert's SHA-1
   fingerprint (the algorithm matches what the gateway re-derives on its side).
4. Sends a `welcome.connect` event so the gateway shows a pending pairing entry
   under a friendly name like `ha-1776370701`.
5. Logs into the gateway, finds that pending entry by friendly name, sets the
   permission flags, and submits the integrity code.
6. Polls the portal for the gateway's `acl-update` push, decrypts the SIP
   password with the private key, parses the door list, and creates one button
   entity per unlock-capable outdoor station.

A successful pairing typically completes in under 15 seconds.

## Entities

For each unlock-capable outdoor station discovered, the integration creates a
`button.<gateway>_<door_name>` entity. Press it from the UI or in an automation:

```yaml
service: button.press
target:
  entity_id: button.abb_welcome_outdoor_1
```

All entities share a single device entry.

The integration also creates:

- `camera.<gateway>_<door_name>` — live intercom stream for each discovered station.
- `switch.<gateway>_streaming_enabled` — arms streaming for a short window; switching it off tears down any active stream.
- `switch.<gateway>_allow_pickup` — allows HA/Scrypted/HomeKit streams to accept an incoming doorbell call. Turning it off leaves manual proactive streaming available outside a ring, but refuses pending INVITE pickup.
- `binary_sensor.<gateway>_intercom_ringing` — turns on briefly when a SIP INVITE/ring is observed.
- `image.<gateway>_latest_screenshot` — latest gateway screenshot from the portal event history. Camera snapshots use station-matched cached screenshots when the portal events can be correlated safely.
- `event.<gateway>_intercom` — event entity for ring / call / door-open history.
- `sensor.<gateway>_last_event` — latest non-screenshot portal event with detailed attributes.
- `sensor.<gateway>_sip_listener` — diagnostic state for the realtime SIP listener.

### Live camera streams

Live camera streams are intentionally gated because opening an ABB intercom media
session can lock the building intercom while the call is active.

To view a stream manually:

1. Turn on `switch.<gateway>_streaming_enabled`.
2. Open the desired `camera.<gateway>_<door_name>` within the armed window.
3. The integration dials the gateway locally and passes H.264 video plus PCMA audio to HA/go2rtc/WebRTC.

When someone rings and `switch.<gateway>_allow_pickup` is on, the integration
auto-arms streaming for a short window so a camera opened from the ring
notification can pick up the pending call without a separate manual step. When
that switch is off, the ring force-disarms streaming and the media pipeline
refuses the pending SIP INVITE, leaving phones and indoor stations free to
answer.

Current HA media support is door station → Home Assistant/browser video and audio.
Talkback is exposed as HA services for the active stream; HomeKit two-way audio is
bridged through Scrypted, which feeds microphone PCM back into those services.

### Talkback

The talkback uplink mode is:

- one continuous Linphone-like audio RTP leg on the same local UDP audio port used
  for the call;
- PCMA / G.711 A-law, static RTP payload type 8;
- 8 kHz mono, 20 ms packets (`a=ptime:20`, 160 PCMA bytes per packet);
- idle/muted state sends PCMA silence continuously;
- push-to-talk swaps queued voice frames into that same RTP sequence, timestamp,
  and SSRC;
- no separate local ABB mute command is required for this path.

The HA-native HomeKit bridge can expose the camera and ring sensor, but it does
not expose a usable microphone path for this custom camera. Use
`scrypted/abb-ha-doorbell` when Apple Home needs two-way audio.

### Apple Home through Scrypted

For a full HomeKit doorbell, use the included Scrypted bridge instead of HA's
native HomeKit camera export:

1. Configure this HA integration first and confirm the HA camera streams work.
2. Install `scrypted/abb-ha-doorbell` in Scrypted.
3. In the Scrypted plugin settings, enter the Home Assistant URL and a
   long-lived access token.
4. Leave the Scrypted **Primary Door Station** setting blank unless you want a
   specific station to keep the existing `front-door` HomeKit identity.
5. Add the Scrypted doorbells to Scrypted's HomeKit plugin.

The Scrypted bridge auto-discovers stations, keeps the HomeKit video/audio
transcode options enabled, forwards HomeKit microphone audio to HA's talkback
services, and refreshes station/RTSP details when this integration fires
`abb_welcome_discovery_changed`.

If Apple Home uses an Apple TV or Home Hub, configure the Scrypted
**HomeKit Pickup Safety** settings. Some hubs open a local preview immediately
after a ring, which can occupy the exclusive ABB intercom call before a person
answers. Assign the hub a fixed LAN IP and enter it in Scrypted if you want
those automatic local previews blocked. Leave the IP field blank to disable
preview blocking. Do not enable Scrypted Rebroadcast/Prebuffer for ABB doorbells
when an Apple TV/Home Hub is present.

### Scrypted RTSP endpoint

For Scrypted/HomeKit, the integration exposes HA's localhost-only go2rtc RTSP
listener through a small LAN TCP proxy. Port selection starts at:

```text
rtsp://<home-assistant-lan-ip>:18556/<go2rtc_stream>
```

`<go2rtc_stream>` is shown on each camera entity as the `go2rtc_stream`
attribute, and the complete current URL is exposed as `lan_rtsp_url`. On setup
the integration tries the saved preferred port first; if that port is occupied,
it automatically scans from `18556` upward, starts on the first free port, and
persists that port in the entry options. A blank advertised host uses Home
Assistant's configured internal/external URL first, then falls back to the local
source address used to reach the ABB gateway.

After the camera entities are loaded, the integration fires
`abb_welcome_discovery_changed` on Home Assistant's event bus with the current
proxy host, port, running state, and change reason. The Scrypted bridge subscribes
to only this ABB event over HA WebSocket so it can refresh `lan_rtsp_url` without
listening to every entity's `state_changed` event.

### Realtime ring event payload

Every incoming SIP ring fires `abb_welcome_ring` on the Home Assistant event bus.
The payload includes both raw SIP caller fields and configured door mapping:

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

Example automation condition:

```yaml
condition:
  - condition: template
    value_template: "{{ trigger.event.data.station_id == '100000001' }}"
```

## Options

After setup, open the integration's **Configure** menu to change behaviour
without removing the entry.

### Unlock strategy

How the integration sends the unlock command to each door. Default is **Hybrid**.

| Strategy | What it does | When to use |
|---|---|---|
| **Hybrid** *(default)* | Plain SIP `MESSAGE` for the first outdoor station, `INVITE`-then-`MESSAGE` for the rest. | Best of both worlds on most setups. |
| **Fast** | Plain SIP `MESSAGE` for every door. | Lowest latency. Some gateways won't accept a `MESSAGE` without an active call session — try this only if Hybrid works for the first door. |
| **Standard** | `INVITE` to bring the call up, then `MESSAGE`, then `BYE`. Same flow as the official mobile app. | Most compatible. Adds ~1-2 seconds per unlock. Switch to this if Hybrid fails for any door on your gateway. |

If a door doesn't open with Hybrid, switch to **Standard** first; if every door
works with **Fast**, you can leave it there for the lowest-latency setup.

## Troubleshooting

- **"Cannot reach the gateway web admin on HTTPS port 443"** — Home Assistant
  cannot open the gateway's local setup interface. Check the IP address and LAN
  routing first.
- **"Invalid portal credentials"** — the MyBuildings portal rejected the
  username or password.
- **"Gateway admin password is wrong"** — the local web admin login at
  `https://<gateway-ip>/` failed. Try logging in manually in a browser to confirm.
- **"Could not read the gateway's portal UUID"** — some firmware versions return
  an empty body for `portalclient.cgi op=6` after login. Fill in the optional
  **Gateway Portal UUID** field and retry; this bypasses that local lookup.
- **"The gateway did not see our pairing request"** — the connect event didn't
  arrive at the gateway in time. Try again; the gateway may have been busy or
  the portal-to-gateway link briefly down.
- **"The gateway rejected the integrity code"** — the cert-fingerprint algorithm
  drifted between gateway firmware and this integration. Please open an issue
  with the firmware version from the gateway's About page.
- **ACL polling timeout** — pairing completed on the gateway side but the
  configuration push did not arrive within ~3 minutes. Try again.
- **A door doesn't open** — switch the unlock strategy (Options → Configure) to
  **Standard** and try again. Only outdoor stations of `type=1` are exported as
  buttons.
- **WebRTC says `wrong response on DESCRIBE`** — make sure `Streaming enabled` is on, then open the camera within the armed window. Version 1.3.0+ also reconnects the SIP dialer automatically if the gateway has closed an idle TLS connection.
- **Camera has video but no audio** — use version 1.2.0-dev15 / 1.3.0 or newer. The stream exposes the gateway's PCMA/G.711 audio track through go2rtc/WebRTC.
- **The camera stops after a short time** — this is expected if the stream consumer closes or the armed switch is turned off. Streaming is deliberately short-lived to avoid holding the building intercom media session open.
- **Apple TV opens the doorbell preview by itself** — in Scrypted, enable
  **Apple TV / Home Hub Present**, keep **Block Apple TV Preview Pickup** on,
  enter the Apple TV/Home Hub fixed LAN IP, and keep Scrypted Rebroadcast/
  Prebuffer disabled for these doorbells.
- **HomeKit can view video but talkback does not work** — make sure the camera
  was added through the Scrypted bridge, not only through HA's native HomeKit
  bridge, and keep Scrypted HomeKit `Transcode Video` and `Transcode Audio`
  enabled.

## Tested hardware

- **ABB 83342 IP Gateway**, firmware `ASM04_GW_V6.25_20250513_MP_TIDM365`,
  system type `mrange`, 3 outdoor stations.

Reports of other models or firmware versions welcome via issues.

## License

MIT — see [LICENSE](LICENSE).
