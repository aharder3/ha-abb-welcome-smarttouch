# SmartTouch 10 implementation notes

This fork supports ABB/Busch-Jaeger SmartTouch 10 as a Welcome IP/SIP bridge.

## Privacy

No installation-specific IP address, portal UUID, MyBuildings username, SIP
username/password, client certificate, private key, outdoor station ID, or other
credential is committed by this patch. All identities are discovered or created
during the Home Assistant config flow and stored only in the HA config entry.

## Protocol path

1. MyBuildings issues a client certificate using the existing integration flow.
2. Discovery accepts both `com.abb.ispf.client.welcome.gateway` and
   `com.abb.ispf.client.welcome.panel`.
3. `welcome.connect` is sent to the selected portal UUID.
4. SmartTouch pairing is completed through the panel/MyBuildings flow, without
   `portalclient.cgi`.
5. The signed `welcome.acl-update` supplies the SIP password, SIP domain, and
   outdoor-station addresses.
6. Runtime media remains local over SIP-TLS/RTP; the existing H.264 + PCMA media
   pipeline is reused unchanged.

## Multiple buildings/devices

If more than one compatible Welcome gateway/panel is visible in the portal
discovery snapshot, setup refuses to guess. Enter the desired Portal UUID in the
optional config-flow field.
