# Source of truth

The source of truth gives hco-core the inventory, the intended config, and the topology. Set it with `HCO_SOT`.

| `HCO_SOT` | Source |
|---|---|
| `yaml` | One inventory file. Use it for a lab and for tests. |
| `nautobot` | Nautobot with the Golden Config app. |

hco-core only reads from the source of truth. A device sync adds and updates the `device` rows. It does not remove a device that is no longer in the source of truth.

## The YAML inventory

Set `HCO_SOT=yaml` and `HCO_SOT_PATH=<path to the file>`. hco-core reads the file again on each request.

```yaml
devices:
  - id: r1
    name: edge-1
    platform_name: CISCO_IOS
    mgmt_address: 10.0.0.1
    tags: [edge, wan]
    intended_config: configs/cisco_ios/intended.txt
    secret: lab
    config_context: {ntp_servers: [192.0.2.10]}
    metadata: {rack: 4}
topology:
  edges:
    - {a: r1, b: s1}
  redundancy_groups:
    core: [s1, x1]
```

- Each device holds the `DeviceRecord` fields: `id`, `name`, `platform_name`, `mgmt_address`, and the optional `role`, `site`, `criticality`, `tags`, `config_format`, `in_maintenance`, and `metadata`.
- `platform_name` must be a platform name that `hier_config` knows, in the exact case, such as `CISCO_IOS`. The error gives the correct name.
- `intended_config` is a path inside the folder of the inventory file.
- `secret` is the key of the device credentials. The default is the device id. See [Secrets](secrets.md).
- Each `metadata` value becomes a string. A list or a mapping is an error.
- Put quotes around a date in `config_context`, such as `"2026-10-16"`. A date without quotes is an error.

The file has these limits:

- It must be UTF-8 text of 1 MiB or less.
- Its nesting must be 32 levels or less.
- It must not use YAML anchors or aliases.

An error names the device id and the key. It never shows a `config_context` or `metadata` value. It can show a platform name or a file path, so that you can find the wrong value.

## Nautobot

Nautobot gives the devices, the intended configs, and the topology. hco-core never writes to Nautobot.

hco-core targets Nautobot 2.1 to 3.x with the Golden Config app 2.x to 3.x. The tests use recorded answers, not a live Nautobot. Do a test against your own Nautobot before you trust the adapter.

Set these variables. Use a read-only API token.

```bash
export HCO_SOT=nautobot
export HCO_NAUTOBOT_URL=https://nautobot.example.com
export HCO_NAUTOBOT_TOKEN=<a read-only Nautobot API token>
```

### Prepare Nautobot

- Set the network driver of each platform. hco-core reads the platform name from `platform.network_driver_mappings["hier_config"]`, such as `ios` or `eos`.
- Give each device a primary IPv4 address.
- Make the intended config of each device with the Golden Config app.
- To make a redundancy group, tag its devices. The tag `hco-redundancy-core` puts each device with that tag in the group `core`. `HCO_NAUTOBOT_REDUNDANCY_TAG_PREFIX` sets the prefix.
- To set the credentials key of a device, set `hco_secret_key` in the config context of the device. See [Secrets](secrets.md).

### What hco-core reads

- Devices, with one GraphQL query (`POST /api/graphql/`) for each page of 500 devices or fewer. The query reads the name, the platform, the role, the location, the tags, the primary IPv4 address, and the cable peer of each interface.
- The intended config, from `GET /api/plugins/golden-config/golden-config/?device=<id>`.
- The config context of a device, with a GraphQL query.
- `GET /api/status/`, for the health check.

hco-core uses these GraphQL names. Make sure that your Nautobot has them:

- `devices(limit:, offset:, id:, tags:)`
- `platform.network_driver_mappings`
- `primary_ip4.host`
- `interfaces.cable_peer_interface.name` and `interfaces.cable_peer_interface.device.id`
- `role.name`, `location.name`, `tags.name`, and `config_context`

### Skipped devices

A device sync skips a device in these conditions:

- The device has no platform.
- The platform has no `hier_config` mapping, or it maps to GENERIC.
- The device has no valid primary IPv4 address.
- The name, the role, the location, or a tag holds a control character, such as a NUL or an escape.

The sync names each skipped device and the reason. The API answer holds `skipped`, a map of the device name to the reason, with 1000 devices or fewer. When the name holds a control character, the map uses the device id. The log line `device_skipped` names every one. The command shows one line for each:

```text
Synced 3 devices.
Skipped 1 device:
  - fw-1: the platform has no hier_config driver mapping
```

### Limits

- The device id is the Nautobot UUID.
- hco-core does not read Nautobot Secrets Groups. See [Secrets](secrets.md).
- The selector applies after the query, so a page can hold fewer devices than the limit.
- Only a cable between two interfaces becomes a topology edge. hco-core does not read circuits.
- The Golden Config answer must hold one row for the device, or no row. Another answer is an error.
- An answer that is larger than 16 MiB, nested more than 32 levels, not UTF-8, or not of the expected shape is an error. An error never quotes Nautobot text.
- The token must be printable ASCII. hco-core never shows it in a log line or an error. hco-core does not follow a redirect.

### Errors

A 401, a 403, another status that is not 2xx, or no answer stops the request. A device sync then answers 503. The log line `request_failed` gives the reason:

| Reason | Cause |
|---|---|
| `EACCES` | Nautobot refused the token. |
| `ENOENT` | Nautobot answered 404. From the Golden Config route, the Golden Config app does not answer. |
| `ETIMEDOUT` | Nautobot did not answer in time. |
| `ECONNREFUSED` | Nautobot refused the connection. |
| `EPROTO` | Nautobot answered with a status that is not 2xx, 401, 403, or 404. |
| `EIO` | The request failed for another reason, and Nautobot gave no answer. |

The `detail` field of the log line gives a sentence, such as `refused the token with HTTP 401; check HCO_NAUTOBOT_TOKEN`.

WARNING: `HCO_NAUTOBOT_VERIFY_SSL=false` stops the check of the TLS certificate. The token can then go to a host that is not your Nautobot. Use this setting only in a lab.
