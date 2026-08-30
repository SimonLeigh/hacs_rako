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

## Credits and licence

This repository formalises, as a HACS custom component, code originally submitted by
[@marengaz](https://github.com/marengaz) in a
[pull request to Home Assistant core (#45915)](https://github.com/home-assistant/core/pull/45915),
which was subsequently abandoned when he lost access to his Rako installation. Since
that PR was against a fork of HA core, this repository inherits its Apache License 2.0
— see [`LICENSE.md`](LICENSE.md).

Issues and pull requests are welcome.
