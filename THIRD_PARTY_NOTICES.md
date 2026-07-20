# Third-party software

## Brother brscan5

This repository includes Brother's unmodified `brscan5` 1.5.1-0 amd64 Debian
package so installation can work without adding a third-party apt repository.

- Package: `third_party/brother/brscan5-1.5.1-0.amd64.deb`
- SHA-256: `7ff1be6e407272843b41ddbf05889ea10cf16b83072a429f8fb443f4dda41ad8`
- Copyright: Brother Industries, Ltd.
- License: [third_party/brother/LICENSE.txt](third_party/brother/LICENSE.txt)

Brother's included license expressly permits reproduction and distribution.
The Brother package is not covered by this repository's MIT license.

## Other runtime dependencies

The daemon uses SANE, bubblewrap, libusb, Pillow, and img2pdf. Those projects
remain under their respective licenses and are installed as system or Python
dependencies; their source or binaries are not copied into this repository.
