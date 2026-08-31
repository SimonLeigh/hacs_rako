# Rako for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/SimonLeigh/hacs_rako/actions/workflows/validate.yml/badge.svg)](https://github.com/SimonLeigh/hacs_rako/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/SimonLeigh/hacs_rako)](LICENSE.md)
![Maintenance](https://img.shields.io/maintenance/yes/2026)

A [Home Assistant](https://www.home-assistant.io/) custom integration for
[Rako Controls](https://rakocontrols.com/) lighting systems, talking directly to a Rako
Bridge on your local network — no cloud dependency.

## What it does

- **Lights and fans**, discovered automatically from the bridge's room/channel/scene
  configuration.
- **Local push**: the integration listens for the bridge's UDP broadcast stream and
  updates entity state as soon as a command or physical keypad press happens, with
  periodic reconciliation against the bridge's cache to catch anything the broadcast
  stream missed.
- **Keypad automation events** — physical keypad presses are fired onto the Home
  Assistant event bus as `rako_event`, so they can be used directly as automation
  triggers (coming in the `0.7.0` release; see the roadmap in
  [`docs/MODERNISATION_PLAN.md`](docs/MODERNISATION_PLAN.md)).

Background on how the Rako Bridge actually behaves on the wire (broadcast semantics,
cache quirks, what does and doesn't count as ground truth) is written up in
[`docs/BRIDGE_BEHAVIOUR.md`](docs/BRIDGE_BEHAVIOUR.md). The wider modernisation plan for
both this repo and the underlying [`python-rako`](https://github.com/SimonLeigh/python-rako)
library lives in [`docs/MODERNISATION_PLAN.md`](docs/MODERNISATION_PLAN.md).

## Installation

### HACS (recommended)

This integration is not yet in the default HACS store, so add it as a custom
repository:

1. In Home Assistant, open **HACS → Integrations**.
2. Click the three-dot menu (top right) → **Custom repositories**.
3. Add `https://github.com/SimonLeigh/hacs_rako`, category **Integration**.
4. Find **Rako** in HACS and click **Download**.
5. Restart Home Assistant.

### Manual

Copy the `custom_components/rako` folder from this repository into your Home
Assistant `config/custom_components/` directory, then restart Home Assistant.

## Configuration

Rako is configured entirely through the Home Assistant UI — there is no YAML
configuration.

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Rako**.
3. If a Rako Bridge is found on your network, its address is pre-populated
   automatically; otherwise enter the bridge's IP address and MAC address manually.

Rooms, channels, scenes and fans are then imported automatically from the bridge.

## Supported Home Assistant versions

This is a personal integration, developed and tested aggressively against current
Home Assistant. Support follows a **rolling ~3-month window, best-effort outside
that**: the tested floor is the newest patch release of the Home Assistant series
from roughly three months before the current stable, and the tested ceiling is
current stable itself. Older Home Assistant versions may still work, but they
aren't tested and aren't a support commitment.

The exact tested floor is `hacs.json`'s `"homeassistant"` key, which HACS also
enforces before letting anyone install onto an older core. CI (`.github/workflows/test.yml`)
runs the full test suite against both ends of that window — `requirements_test_min.txt`
and `requirements_test_latest.txt` — on every push/PR and monthly on a schedule, so the
window keeps moving forward as new Home Assistant releases land.

## Credits and licence

This repository formalises, as a HACS custom component, code originally submitted by
[@marengaz](https://github.com/marengaz) in a
[pull request to Home Assistant core (#45915)](https://github.com/home-assistant/core/pull/45915),
which was subsequently abandoned when he lost access to his Rako installation. Since
that PR was against a fork of HA core, this repository inherits its Apache License 2.0
— see [`LICENSE.md`](LICENSE.md).

Issues and pull requests are welcome.

## How it works (0.7.0 onwards)

The bridge broadcasts a status message for everything it does, but broadcasts
alone are not enough: a listener that dies leaves Home Assistant showing stale
state forever, and nothing is broadcast for changes made while Home Assistant is
down. The integration therefore runs two paths at once.

- **Push.** A supervised UDP listener (auto-restarting, de-duplicating) feeds
  every decoded broadcast into a single state snapshot per bridge.
- **Poll.** Every five minutes by default, the bridge's scene cache is re-read
  over HTTP and merged. A cache-derived level never overwrites a level the
  bridge actually reported, so an app slider change is not undone by a poll.
- **Commands.** Every command is paced (the bridge silently drops commands sent
  too close together) and confirmed by the bridge's own echo. Nothing is
  updated optimistically; a command the bridge never confirms raises an error
  instead of quietly pretending it worked.
- **Availability.** Entities go unavailable only when *both* paths are down.

A level the bridge has not reported -- after a fade, or for a room the bridge
has dropped from its scene cache -- is shown as unknown rather than off, with an
`estimated: true` attribute, and is restored from the entity's last known state
across a restart.

### Keypad events

Every decoded status message is fired on the Home Assistant event bus as
`rako_event`, including fade press/release, occupancy-sensor triggers and
commands the protocol documentation does not describe. That makes a Rako keypad
usable as a trigger for anything in Home Assistant, even buttons that control
nothing Rako-side.

```yaml
automation:
  - triggers:
      - trigger: event
        event_type: rako_event
        event_data:
          room: 9
          command: fade
    actions:
      - action: notify.persistent_notification
        data:
          message: "Someone is holding the fade button in room 9"
```

Payload:

| key | value |
|-----|-------|
| `bridge_mac` | MAC of the bridge that broadcast it |
| `device_id` | Home Assistant device id of the bridge |
| `room`, `channel` | Rako addresses; channel 0 means the whole room |
| `command` | instruction name, e.g. `set_scene`, `fade`, `stop_fading`, `level_toggle`, or `unknown` |
| `command_code` | numeric instruction code (useful when `command` is `unknown`) |
| `data` | raw payload bytes |
| `origin` | `sensor` for occupancy sensors, `control` for keypads/app/HA, `unknown` |
| `scene` | scene number (scene messages) |
| `level` | 0-255 level (level messages) |
| `direction` | `up`/`down` (fade messages) |
| `is_on` | whether the circuit ended up on (level-toggle messages) |

Anything else a particular message carries is included under its own name, so
a message type the library learns to decode later arrives with its payload
intact rather than silently losing it.

The same events are available as device triggers on the bridge device in the
automation editor.

### Options

Configure &rarr; Devices & services &rarr; Rako &rarr; Configure:

- **Reconciliation poll interval** (default 300s, minimum 60s)
- **Command transport** — UDP (default) or HTTP
- **Minimum interval between commands** (default 1.25s, measured against a live
  bridge; commands sent faster than ~1s are silently dropped by the bridge)
