#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="brother-ds640"
SERVICE="brother-ds640.service"
DRIVER_SHA256="7ff1be6e407272843b41ddbf05889ea10cf16b83072a429f8fb443f4dda41ad8"
DRIVER_DEFAULT="$ROOT_DIR/third_party/brother/brscan5-1.5.1-0.amd64.deb"
RULE_TEMPLATE="$ROOT_DIR/packaging/65-brother-ds640.rules.in"
RULE_DESTINATION="/etc/udev/rules.d/65-brother-ds640.rules"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
APP_HOME="$DATA_HOME/$APP"
CONFIG_PATH="$CONFIG_HOME/$APP/config.toml"
STATE_PATH="$STATE_HOME/$APP"
LIB_DIR="$HOME/.local/lib/$APP"
BIN_DIR="$HOME/.local/bin"
VENV="$APP_HOME/venv"
DS640="$VENV/bin/ds640"
SHIM_SOURCE="$ROOT_DIR/native/long_paper_shim.c"
SHIM="$LIB_DIR/libds640_longpaper.so"

OUTPUT_DIR="~/Scans/Inbox"
DRIVER_PACKAGE="$DRIVER_DEFAULT"
ASSUME_YES=0
ACCEPT_LICENSE=0
ENABLE_LINGER=0
NO_START=0
TEST_SCAN=0
DRY_RUN=0
UNINSTALL=0
PURGE=0
SKIP_DEPS=0
INSTALL_COMMITTED=0
SERVICE_WAS_ACTIVE=0
DRIVER_SWAPPED=0
VENV_SWAPPED=0
driver_stage=""
venv_stage=""
venv_new_target=""
venv_previous_target=""
venv_previous_was_directory=0
rule_tmp=""
driver_backup="$APP_HOME/.driver-rollback.$$"
venv_backup="$APP_HOME/.venv-rollback.$$"

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install options:
  --output-dir DIR             PDF destination (default: ~/Scans/Inbox)
  --driver-package FILE        Use this official brscan5 .deb
  --accept-brother-license     Accept third_party/brother/LICENSE.txt
  --enable-linger              Start the user daemon at boot, before login
  --test-scan                  Wait for one physical test sheet during install
  --no-start                   Enable without starting the service now
  --skip-deps                  Do not offer to install missing apt packages
  --yes                        Non-interactive confirmation
  --dry-run                    Print actions without changing the system

Removal:
  --uninstall                  Remove application, service, and udev rule
  --purge                      With --uninstall, also remove config and state

The installer never deletes scanned PDFs.
EOF
}

while (($#)); do
  case "$1" in
    --output-dir) OUTPUT_DIR="${2:?--output-dir requires a value}"; shift 2 ;;
    --driver-package) DRIVER_PACKAGE="${2:?--driver-package requires a value}"; shift 2 ;;
    --accept-brother-license) ACCEPT_LICENSE=1; shift ;;
    --enable-linger) ENABLE_LINGER=1; shift ;;
    --test-scan) TEST_SCAN=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --yes) ASSUME_YES=1; ACCEPT_LICENSE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $EUID -eq 0 ]]; then
  echo "Run this installer as the normal user that will receive scans, not with sudo." >&2
  exit 2
fi
if [[ ! "${USER:-}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
  echo "The current username is not safe for a udev OWNER rule: ${USER:-unset}" >&2
  exit 2
fi

run() {
  printf '+ '
  printf '%q ' "$@"
  printf '\n'
  if [[ $DRY_RUN -eq 0 ]]; then "$@"; fi
}

run_sudo() {
  run sudo "$@"
}

confirm() {
  local prompt="$1"
  if [[ $ASSUME_YES -eq 1 ]]; then return 0; fi
  read -r -p "$prompt [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]]
}

need_sudo() {
  if [[ $DRY_RUN -eq 0 ]]; then
    echo "Administrator access is needed once for USB permissions${1:-}."
    sudo -v
  fi
}

uninstall_app() {
  echo "Removing Brother DS-640 daemon (scanned PDFs will not be touched)."
  if [[ -x "$DS640" ]]; then run "$DS640" service-uninstall || true; fi
  need_sudo " and removal of the udev rule"
  run_sudo rm -f "$RULE_DESTINATION"
  run_sudo udevadm control --reload-rules
  run rm -f "$BIN_DIR/ds640"
  run rm -rf "$VENV" "$APP_HOME"/venv-* "$APP_HOME/driver" "$LIB_DIR"
  if [[ $PURGE -eq 1 ]]; then
    run rm -rf "$CONFIG_HOME/$APP" "$STATE_PATH"
  else
    echo "Preserved configuration: $CONFIG_PATH"
    echo "Preserved state: $STATE_PATH"
  fi
  echo "If enabled solely for this daemon, disable lingering manually:"
  echo "  sudo loginctl disable-linger $USER"
}

if [[ $UNINSTALL -eq 1 ]]; then
  uninstall_app
  exit 0
fi

case "$(uname -m)" in
  x86_64|amd64) ;;
  *) echo "Unsupported architecture: $(uname -m). Brother's bundled driver is amd64 only." >&2; exit 1 ;;
esac
if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify this Linux distribution." >&2
  exit 1
fi
. /etc/os-release
case "${ID:-}" in
  debian|ubuntu|linuxmint) ;;
  *)
    if [[ " ${ID_LIKE:-} " != *" debian "* ]]; then
      echo "Unsupported distribution: ${PRETTY_NAME:-${ID:-unknown}}. Debian-family Linux is required." >&2
      exit 1
    fi
    ;;
esac

missing_commands=()
for command in python3 bwrap scanimage dpkg-deb sha256sum udevadm systemctl cc; do
  command -v "$command" >/dev/null 2>&1 || missing_commands+=("$command")
done
if command -v python3 >/dev/null 2>&1 && ! python3 -m venv --help >/dev/null 2>&1; then
  missing_commands+=("python3-venv")
fi
if ((${#missing_commands[@]})); then
  echo "Missing required commands: ${missing_commands[*]}"
  if [[ $SKIP_DEPS -eq 1 ]]; then exit 1; fi
  if ! confirm "Install required Debian packages now?"; then exit 1; fi
  need_sudo " and system dependencies"
  run_sudo apt-get update
  run_sudo apt-get install -y python3 python3-venv python3-pip bubblewrap sane-utils usbutils dpkg udev gcc libc6-dev
fi

if [[ ! -f "$DRIVER_PACKAGE" ]]; then
  echo "Brother driver package not found: $DRIVER_PACKAGE" >&2
  exit 1
fi
actual_sha="$(sha256sum "$DRIVER_PACKAGE" | awk '{print $1}')"
if [[ "$actual_sha" != "$DRIVER_SHA256" ]]; then
  echo "Brother driver checksum mismatch." >&2
  echo "Expected: $DRIVER_SHA256" >&2
  echo "Actual:   $actual_sha" >&2
  exit 1
fi
if [[ "$(dpkg-deb -f "$DRIVER_PACKAGE" Package)" != "brscan5" ]] ||
   [[ "$(dpkg-deb -f "$DRIVER_PACKAGE" Architecture)" != "amd64" ]]; then
  echo "The supplied package is not the expected amd64 Brother brscan5 driver." >&2
  exit 1
fi

if [[ $ACCEPT_LICENSE -eq 0 ]]; then
  echo "Brother's driver is covered by: $ROOT_DIR/third_party/brother/LICENSE.txt"
  if ! confirm "Accept the Brother License Agreement?"; then exit 1; fi
fi

need_sudo " and the persistent scanner udev rule"
run mkdir -p "$APP_HOME" "$CONFIG_HOME/$APP" "$STATE_PATH" "$LIB_DIR" "$BIN_DIR"

echo "+ cc -shared -fPIC -O2 -Wall -Wextra -o $SHIM $SHIM_SOURCE -ldl"
if [[ $DRY_RUN -eq 0 ]]; then
  cc -shared -fPIC -O2 -Wall -Wextra -o "$SHIM" "$SHIM_SOURCE" -ldl
fi

if [[ $DRY_RUN -eq 0 ]]; then
  if systemctl --user is-active --quiet "$SERVICE"; then SERVICE_WAS_ACTIVE=1; fi

  rollback_install() {
    local exit_code=$?
    trap - EXIT
    if [[ -n "${driver_stage:-}" ]]; then rm -rf "$driver_stage"; fi
    if [[ -n "${venv_stage:-}" ]]; then rm -rf "$venv_stage"; fi
    if [[ -n "${rule_tmp:-}" ]]; then rm -f "$rule_tmp"; fi
    rm -f "$APP_HOME/.venv-link-new.$$"
    if [[ $INSTALL_COMMITTED -eq 0 ]]; then
      echo "Installation failed; restoring the previous application." >&2
      if [[ $DRIVER_SWAPPED -eq 1 ]]; then
        rm -rf "$APP_HOME/driver"
        if [[ -d "$driver_backup" ]]; then mv "$driver_backup" "$APP_HOME/driver"; fi
      fi
      if [[ $VENV_SWAPPED -eq 1 ]]; then
        rm -rf "$VENV"
        if [[ -n "$venv_new_target" ]]; then rm -rf "$venv_new_target"; fi
        if [[ -n "$venv_previous_target" ]]; then
          if [[ $venv_previous_was_directory -eq 1 ]]; then
            mv "$venv_previous_target" "$VENV"
          else
            ln -s "$venv_previous_target" "$VENV"
          fi
        fi
      fi
      if [[ $SERVICE_WAS_ACTIVE -eq 1 ]]; then
        systemctl --user start "$SERVICE" >/dev/null 2>&1 || true
      fi
    fi
    exit "$exit_code"
  }
  trap rollback_install EXIT

  driver_stage="$(mktemp -d "$APP_HOME/.driver-new.XXXXXX")"
  dpkg-deb -x "$DRIVER_PACKAGE" "$driver_stage"
  [[ -f "$driver_stage/opt/brother/scanner/brscan5/libsane-brother5.so.1.0.7" ]]

  venv_stage="$APP_HOME/venv-$(date +%Y%m%d-%H%M%S)-$$"
  venv_new_target="$venv_stage"
  python3 -m venv "$venv_stage"
  "$venv_stage/bin/python" -m pip install --disable-pip-version-check --upgrade "$ROOT_DIR"

  rm -rf "$driver_backup" "$venv_backup"
  if [[ -d "$APP_HOME/driver" ]]; then mv "$APP_HOME/driver" "$driver_backup"; fi
  mv "$driver_stage" "$APP_HOME/driver"
  driver_stage=""
  DRIVER_SWAPPED=1

  if [[ -L "$VENV" ]]; then
    venv_previous_target="$(readlink -f "$VENV")"
  elif [[ -d "$VENV" ]]; then
    mv "$VENV" "$venv_backup"
    venv_previous_target="$venv_backup"
    venv_previous_was_directory=1
  fi
  ln -s "$venv_new_target" "$APP_HOME/.venv-link-new.$$"
  mv -Tf "$APP_HOME/.venv-link-new.$$" "$VENV"
  venv_stage=""
  VENV_SWAPPED=1

else
  echo "+ dpkg-deb -x $DRIVER_PACKAGE $APP_HOME/driver"
  echo "+ build isolated virtual environment at $VENV"
fi

run ln -sfn "$DS640" "$BIN_DIR/ds640"

if [[ ! -f "$CONFIG_PATH" ]]; then
  run "$DS640" --config "$CONFIG_PATH" init-config --output-dir "$OUTPUT_DIR"
else
  echo "Preserving existing configuration: $CONFIG_PATH"
fi

if [[ $DRY_RUN -eq 0 ]]; then
  rule_tmp="$(mktemp)"
  sed "s/@INSTALL_USER@/$USER/g" "$RULE_TEMPLATE" >"$rule_tmp"
  sudo install -o root -g root -m 0644 "$rule_tmp" "$RULE_DESTINATION"
  rm -f "$rule_tmp"
  rule_tmp=""
else
  echo "+ install udev rule for user $USER at $RULE_DESTINATION"
fi
run_sudo udevadm control --reload-rules
run_sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=04f9 --attr-match=idProduct=0468

if [[ $ENABLE_LINGER -eq 1 ]]; then run_sudo loginctl enable-linger "$USER"; fi

if [[ $DRY_RUN -eq 0 ]]; then
  if [[ $SERVICE_WAS_ACTIVE -eq 1 ]]; then systemctl --user stop "$SERVICE"; fi
  if "$DS640" --config "$CONFIG_PATH" discover; then
    "$DS640" --config "$CONFIG_PATH" doctor || {
      echo "Health check failed. Review the output above before relying on the daemon." >&2
      exit 1
    }
    if [[ $TEST_SCAN -eq 1 ]]; then
      "$DS640" --config "$CONFIG_PATH" test-scan --timeout 120
    fi
  else
    echo "No connected DS-640 was found. Installation will continue; run 'ds640 doctor' after connecting it."
  fi
fi

service_args=()
if [[ $NO_START -eq 1 ]]; then service_args+=(--no-start); fi
run "$DS640" --config "$CONFIG_PATH" service-install "${service_args[@]}"

if [[ $DRY_RUN -eq 0 ]]; then
  INSTALL_COMMITTED=1
  rm -rf "$driver_backup" "$venv_backup"
  if [[ -n "$venv_previous_target" && $venv_previous_was_directory -eq 0 &&
        "$venv_previous_target" == "$APP_HOME"/venv-* &&
        "$venv_previous_target" != "$venv_new_target" ]]; then
    rm -rf "$venv_previous_target"
  fi
fi

echo
echo "Brother DS-640 daemon installed."
echo "Configuration: $CONFIG_PATH"
echo "Scans: $OUTPUT_DIR"
echo "Status: $BIN_DIR/ds640 service-status"
echo "Logs: journalctl --user -u $SERVICE -f"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "Note: add $BIN_DIR to PATH, or open a new login shell before using 'ds640'."
fi
