# Brother DS-640 always-ready scanner daemon

This Linux daemon keeps a USB-connected Brother DS-640 awake, automatically
grabs an inserted sheet, and writes a PDF without opening a scanning app.

It polls Brother's paper-presence sensor, starts one normal scan when a sheet
is inserted, and returns to waiting. Normal scans use automatic document sizing
to remove the unused scanner area. No ATCN protocol patch or preload shim is
needed. USB vendor/product identity follows the scanner across ports.

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
5. installs the paper-sensor worker and scan daemon;
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
auto_document_size = true
scan_timeout_seconds = 60.0
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

Automatic document sizing is enabled by default. It works with ordinary scans;
version 1.2 replaces the old ATCN continuous-feed workaround that prevented it.
Set `auto_document_size = false` if you deliberately need the full scan area.
Auto sizing requires 400 DPI or lower on this model; the default is 300 DPI.

The paper query uses an undocumented API in the bundled, checksum-verified
brscan5 1.5.1-0 driver. A separate worker contains driver failures and polls at
`poll_seconds` (minimum 0.1 seconds). It closes its device handle before the
one-page scan starts. Idle query workers are renewed every 30 seconds, and USB
discovery is repeated between workers and scans. Query errors are retried;
unknown sensor states never trigger a scan. Scans time out after
`scan_timeout_seconds`. After a failed scan, remove the sheet before reinserting
it: the daemon waits for the feeder to clear instead of repeatedly feeding a jam.

Old `atcn_timeout`, `atcn_feed_delay`, and `tap_library` config keys are accepted
for upgrade compatibility but are unused. `part_settle_seconds` applies only to
recovery of files left by older ATCN versions.

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
- Only completed normal scans are published. Partial failed scans remain in the
  recovery directory and are not silently accepted as complete receipts.
- Recovery of unfinished PNG streams from older ATCN versions is retained.
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
