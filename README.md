# Brother DS-640 always-ready scanner daemon

This Linux daemon keeps a USB-connected Brother DS-640 awake, automatically
grabs an inserted sheet, and writes a PDF without opening a scanning app.

It uses Brother's own undocumented auto-continuous (`ATCN`) protocol mode. The
scanner is identified by USB vendor/product ID—not by a bus number or physical
port—so reconnecting it elsewhere does not require reconfiguration.

## Supported systems

- Debian, Ubuntu, Linux Mint, and compatible Debian-family distributions
- x86-64/amd64
- systemd user sessions
- Brother DS-640 (`04f9:0468`)

Brother only provides this `brscan5` build for amd64. The installer rejects
unsupported architectures instead of attempting a partial installation.

## Install

Clone the repository and run the installer as your normal user:

```bash
git clone <repository-url> brother-ds640-daemon
cd brother-ds640-daemon
./install.sh
```

Do not run the script itself with `sudo`. It asks for administrator
authentication once to install missing system dependencies and the persistent
USB permission rule. The daemon and every scan run as your normal user.

The installer:

1. checks the distribution and CPU architecture;
2. offers to install missing Debian packages;
3. verifies the bundled Brother driver checksum and license;
4. extracts the driver privately under `~/.local/share/`;
5. builds the small ATCN protocol shim;
6. creates the editable configuration;
7. discovers the scanner on any USB port and runs a health check;
8. installs and starts a systemd user service.

For a non-interactive install:

```bash
./install.sh --yes
```

To include a physical one-page installation test:

```bash
./install.sh --test-scan
```

To start the daemon at boot even before login:

```bash
./install.sh --enable-linger
```

That setting is optional. Without it, the daemon starts automatically when the
user logs in.

## Configuration

The installer creates (under `$XDG_CONFIG_HOME` when set):

```text
~/.config/brother-ds640/config.toml
```

It is ordinary TOML. The main settings are:

```toml
[scanner]
resolution = 300
mode = ""
source = ""
extra_args = []
part_settle_seconds = 5.0

[output]
directory = "~/Scans/Inbox"
filename_prefix = "scan"
keep_images = false
```

An empty mode or source uses Brother's device default. Values exposed by this
model include `24bit Color[Fast]`, `Black & White`, `True Gray`, and
`Gray[Error Diffusion]`.

### Receipts and other small documents

Brother's automatic document-size option is intentionally not used. On the
DS-640 it can pull a page into position but then stall image transfer when
combined with the ATCN continuous-feed mode that keeps this daemon waiting.
The output therefore uses Brother's normal full scan area.

The DS-640's feeder is left-aligned. Put a small document against the left edge
of the feeder slot and move the document guide against its right edge. Brother
recommends using a DS-series Carrier Sheet for receipts, clippings, photos, and
other non-standard or flimsy originals: place the receipt at the carrier
sheet's top-left corner, then feed the carrier sheet header-first and
left-aligned. Software cannot make a receipt that misses the physical feed
sensor engage the rollers.

After editing the file, restart the service:

```bash
systemctl --user restart brother-ds640.service
```

See [config.example.toml](config.example.toml) for every setting.

## Everyday commands

```bash
ds640 discover
ds640 doctor
ds640 service-status
journalctl --user -u brother-ds640.service -f
```

Run in the foreground for troubleshooting:

```bash
systemctl --user stop brother-ds640.service
ds640 run
```

Validate one physical sheet:

```bash
systemctl --user stop brother-ds640.service
ds640 test-scan
systemctl --user start brother-ds640.service
```

## USB discovery and reconnects

Linux assigns transient names such as `/dev/bus/usb/002/004`. The daemon never
stores that name. Each session scans sysfs for `04f9:0468`, checks access to the
current device node, and lets Brother's backend enumerate the scanner again.

The installed udev rule also matches `04f9:0468`, so it follows the device
across ports and reconnects. It grants access only to the user who ran the
installer rather than making the device world-writable.

If more than one identical DS-640 is connected, the daemon refuses to guess.
Multi-scanner selection is not currently supported by the Brother backend
wrapper.

## Reliability behavior

- Empty-feeder and disconnected-device states are retried indefinitely.
- Brother's ATCN session is automatically re-armed when it expires.
- Completed PDFs are written to a hidden temporary file and atomically renamed.
- The active ATCN page is recoverable even though Brother leaves its PNG stream
  without a final trailer while waiting for another sheet.
- Delivery markers and deterministic filenames prevent duplicates after a
  daemon crash.
- Undelivered scan data is retained rather than silently deleted.
- Service logs go to journald, which handles rotation.

## Updates

Pull a newer checkout and run `./install.sh` again. The installer replaces the
application and private driver runtime while preserving the existing config,
state, and scanned PDFs.

## Uninstall

```bash
./install.sh --uninstall
```

This removes the service, application, private driver, and udev rule. It keeps
configuration, recovery state, and all PDFs. To remove config and recovery
state as well:

```bash
./install.sh --uninstall --purge
```

Scanned PDFs are never removed.

## Driver licensing

The daemon code is MIT licensed. Brother's separately identified `brscan5`
package remains under Brother's license, which permits redistribution. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
[third_party/brother/LICENSE.txt](third_party/brother/LICENSE.txt).

This project is independent and is not affiliated with or endorsed by Brother
Industries, Ltd.

## Development

Run the unit tests without scanner hardware:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

Check an installation without making changes:

```bash
./install.sh --dry-run --yes
```
