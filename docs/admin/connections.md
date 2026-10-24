# Device connections

The device connection reads the running config and the device state. Set it with `HCO_CONNECTION`.

| `HCO_CONNECTION` | Connection |
|---|---|
| `mock` | Reads transcript files. Use it for a lab and for tests. |
| `scrapli` | Reads from a real device over SSH. |

No connection writes to a device in Phase 1. Each connection refuses a write. Phase 4 adds device writes.

## The mock connection

The mock connection reads files from `HCO_TRANSCRIPTS_DIR`:

- `<HCO_TRANSCRIPTS_DIR>/<device id>/running.txt` holds the running config.
- One file for each command holds its answer, such as `show_version.txt`.

A device with no transcript folder is unreachable.

## The scrapli connection

Install the extra, then set the connection:

```bash
pip install 'hco-core[scrapli]'
export HCO_CONNECTION=scrapli
```

### Platforms

| hco-core platform | scrapli platform |
|---|---|
| `CISCO_IOS` | `cisco_iosxe` |
| `CISCO_NXOS` | `cisco_nxos` |
| `CISCO_XR` | `cisco_iosxr` |
| `ARISTA_EOS` | `arista_eos` |

The driver uses the CLI format only.

### Prepare the host

The driver connects to the management address of the device on port 22. It uses the `ssh` command of the host, and it checks the host key.

1. Log in as the user that runs hco-core.
2. Add the host key of each device to the known hosts file of that user.
3. Set the credentials of each device. See [Secrets](secrets.md).

The driver refuses an address that is empty, starts with `-`, holds a space, or holds a character that is not printable.

### What the driver sends

- `show running-config`, to read the running config.
- The fingerprint command of the platform, to detect a config change.
- The commands of the read-only allowlist of the platform, to read the device state. The driver refuses any other command before it sends it.

Each allowlist command is a `show` command in printable ASCII. After a pipe, only `include`, `exclude`, `begin`, and `section` are permitted.

### Behavior

- A session runs one command at a time. Two sessions to one device use two connections.
- A connection error stops the read. The log line `device_unreachable` gives the device, the error type, and the error text.
- A refused command, or an answer over the size limit, also stops the read. Its reason is only in the run error.
- hco-core makes the error text printable and cuts it at 200 characters. It removes the user name, the password, and the enable secret from the text. The text can hold the device prompt. It never holds a command answer.
- A CLI error of the platform, such as `% Invalid input detected`, counts only on the first line of an answer. The same text in a description or a banner does not stop a read.
- hco-core refuses an answer larger than 16,777,216 characters, and it never keeps that answer. The session timeout limits the time of each read.
