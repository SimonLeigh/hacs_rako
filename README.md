# Notice

This repository is intended to formalise as a HACS custom component the code submitted in a PR to homeassistant core by @marengaz but subsequently abandoned when he lost access to his Rako system: https://github.com/home-assistant/core/pull/45915

Since this was a PR from a fork of HA core, I believe that the Apache 2 license should be inherited.

## Why?

There are a few users of Rako lighting (including me) who have successfully used this code, but it is floating around in zip files on the forum (https://community.home-assistant.io/t/rako-lighting/3121) and relatively hidden in an abandoned PR.

So, in order to make this easier to maintain, I have migrated the code into a HACS'ified' form.

## Next steps

- Validate that this repository can be used as a custom-repo in hacs to easily allow access to this repo.
- Bring some of the Python "up-to-date" and make sure that near-future versions of HA don't break compatability with the repo.
- Increase coverage of the Rako API to allow things like fans (which I have in my Rako deployment but which aren't imported by the current implementation)
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

The same events are available as device triggers on the bridge device in the
automation editor.

### Options

Configure &rarr; Devices & services &rarr; Rako &rarr; Configure:

- **Reconciliation poll interval** (default 300s, minimum 60s)
- **Command transport** — UDP (default) or HTTP
- **Minimum interval between commands** (default 1.25s, measured against a live
  bridge; commands sent faster than ~1s are silently dropped by the bridge)
