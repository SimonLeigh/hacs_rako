# Rako Modernisation & State-Sync Plan

**Owner repo:** `hacs_rako` (this repo) — governs `python-rako` (published as `python-rako-2025` on PyPI).
**Date:** 2026-08-24
**Status:** Phase 0 interactive characterisation COMPLETE (2026-08-25) — see
`docs/BRIDGE_BEHAVIOUR.md`. Design in §3 updated with its outcomes; Phases 1–3 not started.
**Maintainer:** Simon Leigh (@SimonLeigh)

---

## 1. Goals

1. Bring `python-rako` up to modern Python (3.12/3.13) library standards and publish a new
   `python-rako-2025` release to PyPI with a working, secure release pipeline.
2. Bring `hacs_rako` up to current Home Assistant integration standards
   (runtime_data, coordinator pattern, tests, translations, HA Quality Scale — target Silver).
3. **Fix state divergence**: redesign state tracking so the HA-tracked state cannot silently
   drift from the real Rako controller state, with live validation against the maintainer's live installation.

## 2. Current-state assessment

### 2.1 python-rako (`../python-rako`, publishes `python-rako-2025`, currently 0.4.1)

Already good: hatchling + pyproject, ruff, mypy, pytest suite with XML fixtures,
`tests_integration/` live-bridge tests, py.typed, CI on 3.12/3.13.

Problems found (ordered by severity):

| # | Problem | Where |
|---|---------|-------|
| L1 | **Release workflow writes a bare tag into `__version__.py`** (`echo "$RELEASE_TAG" > python_rako/__version__.py`) — produces an invalid Python file; hatchling `[tool.hatch.version]` cannot parse it. Releases are broken as configured. | `.github/workflows/release.yml` |
| L2 | **UDP command timeout treated as success** (`return True` on 3s recv timeout). Callers optimistically update state on a command that may never have arrived → divergence seed. | `bridge.py` `BridgeCommanderUDP._send_command_with_retry` |
| L3 | **Blocking XML parse on the event loop**, wrapped in a `threading.Lock` (the lock does nothing useful in an event loop; the blocking parse is an HA-standards violation for large rako.xml files). Should be parsed via `asyncio.to_thread` / executor, lock removed. | `bridge.py` `_XML_PARSE_LOCK`, `get_bridge_info_from_discovery_xml`, `get_devices_from_discovery_xml` |
| L4 | **HTTP commander never checks the response** (`await session.post(...)` — no `raise_for_status`, no result inspection). Silent failure → divergence seed. | `bridge.py` `BridgeCommanderHTTP._send_command` |
| L5 | `refresh_cache_if_stale()` exists but has no consumer; `UDPMessageRateLimit`, `get_predicted_channel_brightness`, `RakoCommandError` are dead code. Either wire them into the new sync design or delete. | `bridge.py`, `helpers.py`, `exceptions.py` |
| L6 | No structured "state snapshot" API. `get_cache_state()` returns raw caches; consumers must know that *current room scene* (SceneCache) × *scene level table* (LevelCache) ⇒ derived channel levels. This derivation logic lives (badly) in the HA integration. | API design gap |
| L7 | Discovery (`discover_bridge()`) has no timeout of its own and leaks the socket on the happy path (no close). | `__init__.py` |
| L8 | `pyproject.toml` URLs point at `github.com/simonleigh/python-rako` — verify against the real repo slug/casing. `TCH` ruff code is renamed `TC` in current ruff; pin/refresh lint config. | `pyproject.toml` |
| L9 | CI matrix stops at 3.13; HA 2025.x runs Python 3.13 — add 3.13 as primary, consider 3.14 when HA adopts it. Consider `uv` for CI speed. | `.github/workflows/ci.yml` |

### 2.2 hacs_rako (custom component `rako`, currently 0.6.0)

Already good: config flow with UDP auto-discovery, push-based entities, HACS packaging,
light + fan platforms, hassfest/HACS validation workflows.

Problems found:

| # | Problem | Where |
|---|---------|-------|
| H1 | **Listener loop has zero error handling.** Any socket error / parse exception kills `listen_for_state_updates` silently. HA keeps showing stale state forever, entities stay "available". This is the primary suspected divergence mechanism together with plain UDP loss. | `bridge.py` `listen_for_state_updates` |
| H2 | **No reconciliation poll.** UDP status broadcasts are lossy (Wi-Fi, container NAT, bridge restarts, changes made while HA is down). Nothing ever re-reads the bridge cache after startup. | architecture |
| H3 | **`hass.data` keyed inconsistently**: setup stores under `rako_bridge.mac`, unload deletes `hass.data[DOMAIN][entry.unique_id]`. Works only while they coincide. Modern standard: typed `entry.runtime_data`, no `hass.data` at all. | `__init__.py` |
| H4 | Deprecated/incorrect API usage: `FlowResult` → `ConfigFlowResult`; `async_forward_entry_unload` per-platform → `async_unload_platforms`; `DeviceInfo` returned as bare dict; `should_poll` property → `_attr_should_poll = False`. | `config_flow.py`, `__init__.py`, `light.py`, `fan.py` |
| H5 | Entity-type duck typing (`hasattr(entity, "brightness")`) to route updates; `dict[str, any]` (lowercase `any` — genuine bug, `any` is the builtin function); absolute import `from custom_components.rako.fan import ...` creating an import cycle with `light`/`fan`. | `bridge.py`, `model.py` |
| H6 | Listener lifecycle tied to first/last entity registration instead of entry setup/unload; no supervision, no restart, no availability signalling. | `bridge.py` |
| H7 | **Zero tests.** HA custom integrations are expected to test with `pytest-homeassistant-custom-component` (config flow, setup/unload, entity behaviour, coordinator). | repo |
| H8 | **Release workflow is broken** — still references `integration_blueprint` paths from the template; zips a directory that doesn't exist. | `.github/workflows/release.yml` |
| H9 | Entity naming predates `has_entity_name`; no `_attr_translation_key`; manifest lacks `loggers`, `quality_scale`; `strings.json`/`en.json` drift risk (three copies of the same strings). | `light.py`, `fan.py`, `manifest.json` |
| H10 | Scene→channel fan-out in `_state_update` recursively synthesises channel updates from **whatever LevelCache was loaded at startup** — stale if scenes are re-programmed. Cache is loaded once in `light.async_setup_entry` and never refreshed (see L5/H2). | `bridge.py` |
| H11 | `requirements.txt` pins `homeassistant==2025.2.4` for dev while `hacs.json` claims min `2025.5.0` — inconsistent; refresh both to a current baseline. | repo |

## 3. Target architecture (the state-sync fix)

### 3.1 Known divergence causes

1. Missed UDP status broadcasts (lossy transport, container networking, bridge under load).
2. Listener task death (H1) — permanent silent staleness.
3. Changes made while HA is down/restarting.
4. Optimistic updates on commands that silently failed (L2, L4).
5. Stale scene→level table used for scene fan-out (H10).
6. (To verify in Phase 0) changes made via Rako app/cloud or certain keypad events that
   the bridge may not broadcast at all.

### 3.2 Design: hybrid push + reconciliation coordinator

**In python-rako** — add a first-class state API so the HA layer stops deriving state itself:

- `BridgeStateSnapshot` model: per-room current scene + per-channel *derived current level*
  (SceneCache × LevelCache resolution moves into the library, replacing H10's ad-hoc logic).
- `Bridge.get_state_snapshot() -> BridgeStateSnapshot` — wraps `get_cache_state()` + derivation.
- A supervised `StatusListener` helper: owns the datagram socket, auto-restarts with
  exponential backoff, exposes `last_message_at`, and surfaces health via a callback —
  so *every* consumer of the library gets a robust listener, not just HA.
- Command verification: `set_*` methods gain a `verify: bool = True` mode — after sending,
  await the echoed status broadcast for ~1.5 s; on silence, raise/flag instead of
  pretending success (replaces L2's `return True`-on-timeout).

**In hacs_rako** — a `RakoCoordinator` (subclassing `DataUpdateCoordinator[BridgeStateSnapshot]`):

- **Push path:** the supervised listener feeds status messages into the coordinator, which
  applies them to its snapshot and notifies entities (`async_set_updated_data`).
- **Poll path (reconciliation):** `update_interval` (default 5 min, configurable via a new
  options flow) re-fetches the snapshot from the bridge and *replaces* tracked state —
  bounding divergence at one poll interval no matter what the push path missed.
- **Post-command reconcile:** after any service call, if no echo broadcast arrives within
  the verify window, trigger an immediate targeted refresh for that room.
- **Availability:** listener down *and* poll failing ⇒ entities unavailable (today they
  stay "on and lying").
- Entities become thin `CoordinatorEntity` views over the snapshot — no more per-entity
  registration maps, no duck typing, no `hass.data`.

### 3.2a Phase 0 outcomes that change the design (2026-08-25)

Full evidence in `docs/BRIDGE_BEHAVIOUR.md`. Consequences for §3.2, in priority order:

1. **Echo-verify + retry is the PRIMARY reliability mechanism, not polling.** Every real
   state change the bridge performed was broadcast (0 broadcast losses observed); the
   losses observed (2 in one evening) were *commands that never took effect*. Echo arrives
   in 150–300 ms — faster than the bridge's own AOK (~750 ms). Library `set_*` methods:
   send → await matching echo ≤1.5 s → on silence retry once → on second silence raise
   `RakoCommandError`; entity state updates ONLY from the echo, never optimistically.
2. **Decoder must handle everything the bridge sends.** Add FADE (0x32, with direction
   flag), STOP (0x0F), STORE (0x0D), IDENT (0x08), and the undocumented 0x33 (observed
   `[flags, level, on/off]`); unknown commands become a typed `UnknownStatusMessage`
   (room/channel/command/data) that is logged at debug and *still* delivered to
   consumers — never silently dropped.
3. **Per-channel provenance in the state model.** Cache-derived (scene×level-table) state
   is an approximation; LEVEL_SET/SET_LEVEL broadcasts carry true levels. Reconciliation
   applies cache-derived levels **only when the cached scene differs from the tracked
   scene** (a missed scene change). It must never overwrite a fresher level broadcast.
   After a FADE/STOP pair the level is genuinely unknown → state = `unknown` (HA shows
   the light as on/off from last known, with an "estimated" attribute), refined by any
   later broadcast. Fade-rate estimation is optional (open item).
4. **Startup state must not assume cache-absent rooms are off.** Fade-controlled rooms
   are deleted from the scene cache by design. Initialise absent rooms from HA's
   restored last state (`RestoreEntity`) marked estimated; not as "off".
5. **Reconcile over HTTP `scenes.htm`**, not the UDP cache query, so polling never
   contends with the listener socket. Poll interval default 5 min remains sensible;
   the level table (UDP `LEVEL_CACHE`) is refreshed only at startup and on STORE
   broadcasts (scene re-programmed).
6. **Keypad presses become HA events** (new deliverable, WP-2.6): every decoded status
   message fires `rako_event` on the HA bus (`{bridge, room, channel, command, data,
   direction}`) and is exposed as device triggers, so automations can react to keypad
   buttons — including ones mapped to nothing HA tracks.

### 3.3 Phase 0 — live characterisation (COMPLETE except overnight soak)

Short scripted experiments against the live bridge (extend `tests_integration/`):

- [ ] Does the bridge broadcast status for: HA-sent UDP commands? HA-sent HTTP commands?
      physical keypad presses? Rako app (local)? Rako app (cloud/away)? scheduled events?
- [ ] Does `SCENE_CACHE`/`LEVEL_CACHE` reflect manual channel-level tweaks, or only scene
      selections? How fast does the cache update after a change?
- [ ] What does the bridge report for a channel set to a non-scene level (e.g. dimmed via
      app slider)? Does the snapshot derivation hold, or do we need to track
      `ChannelStatusMessage` levels as overrides on top of the scene-derived baseline?
- [ ] Measured broadcast-echo latency after a command (calibrates the verify window).
- [ ] Behaviour of two concurrent listeners on port 9761 (dev HA + prod HA both listening).

Results go in `docs/BRIDGE_BEHAVIOUR.md` and gate the 3.2 design details.

## 4. Work packages & agent delegation

Model guidance: **Opus** for architecture/concurrency-sensitive packages, **Sonnet** for
mechanical modernisation, tests scaffolding, CI, and docs. Every package lands as a PR
with tests; nothing merges red.

### Phase 1 — python-rako v0.5.0

| WP | Scope | Agent | Depends on |
|----|-------|-------|-----------|
| WP-1.1 | Fix release pipeline: correct `__version__.py` generation, migrate to PyPI **Trusted Publishing** (drop token), add TestPyPI dry-run job, build attestations | Sonnet | — |
| WP-1.2 | Mechanical modernisation: executor-based XML parsing (drop `_XML_PARSE_LOCK`), discovery timeout + socket cleanup, dead-code removal or wiring (L5), ruff/mypy config refresh, CI matrix refresh | Sonnet | — |
| WP-1.3 | **State API**: `BridgeStateSnapshot` with per-channel provenance, `get_state_snapshot()` (via `scenes.htm`), supervised `StatusListener` with backoff + health, **echo-verified commands with retry** (§3.2a-1), **full decoder** incl. FADE/STOP/STORE/IDENT/0x33/unknown (§3.2a-2), 10-bit room parsing. Unit tests with fake UDP endpoints replaying the captured broadcasts in `docs/BRIDGE_BEHAVIOUR.md` | **Opus** | Phase 0 ✅ |
| WP-1.4 | Integration tests refresh (`tests_integration/`) incl. Phase-0 characterisation scripts, kept runnable against a live bridge | Sonnet | WP-1.3 |
| WP-1.5 | Release `python-rako-2025==0.5.0` to PyPI (changelog, tag, verify install) | Sonnet | WP-1.1–1.4 |
| WP-1.6 | **Command pacing queue** (maintainer requirement, 2026-08-29): a per-bridge FIFO `CommandQueue` in the library that enforces a minimum interval between sends to the bridge (default **1.5 s** until measured — see open item below; configurable). Requests arriving faster than the interval are queued, not dropped, and executed in order once the interval has elapsed. Same-target coalescing: if several commands for the same (room, channel) are waiting, only the latest is sent (a slider drag becomes one command), while commands for different targets keep their order. Integrates with echo-verify: the next send waits for the previous echo OR the interval, whichever is later. Expose queue depth/oldest-age for diagnostics; HA's coordinator uses this instead of calling `set_*` directly. Live measurement task: find the real minimum safe spacing by sending paced command pairs to one channel at decreasing intervals and recording the first interval at which an echo is missed; write the result to BRIDGE_BEHAVIOUR.md and make it the default. | **Opus** | WP-1.3 ✅ |

### Phase 2 — hacs_rako v0.7.0

| WP | Scope | Agent | Depends on |
|----|-------|-------|-----------|
| WP-2.1 | Plumbing modernisation: `entry.runtime_data` (typed `RakoConfigEntry`), `async_unload_platforms`, `ConfigFlowResult`, relative imports, `DeviceInfo` objects, `_attr_*` conventions, fix H3/H4/H5 | Sonnet | — |
| WP-2.2 | **`RakoCoordinator`** per §3.2: push+poll hybrid, post-command reconcile, availability. Entities rewritten as `CoordinatorEntity`. Listener lifecycle owned by entry setup/unload | **Opus** | WP-1.3 (needs 0.5.0) |
| WP-2.3 | Options flow (poll interval, UDP vs HTTP command transport), `has_entity_name` + translation keys migration (document entity-name change for users), manifest polish (`loggers`, `quality_scale`) | Sonnet | WP-2.2 |
| WP-2.4 | Test suite with `pytest-homeassistant-custom-component`: config flow, setup/unload, coordinator push & poll paths, divergence-recovery scenarios (dropped broadcast, dead listener, bridge reboot) | Sonnet (Opus reviews coordinator tests) | WP-2.2 |
| WP-2.5 | Fix release workflow (zip the real `custom_components/rako`, stamp manifest version), refresh `requirements.txt`/`hacs.json` baseline (H8/H11), README refresh | Sonnet | — |
| WP-2.6 | **Keypad events**: fire `rako_event` on the HA event bus for every decoded status message; register device triggers (per bridge: room/channel/command) so keypad presses drive automations for non-Rako systems (§3.2a-6) | Sonnet (Opus reviews event schema) | WP-2.2 |

### Phase 3 — live validation (with the maintainer)

**Environment constraint (confirmed 2026-08-25):** the production HA instance runs in
Docker with host networking and its current Rako integration binds UDP 9761 *without*
reuse flags, so no second listener can bind on that host. Consequences:
- The dev HA instance must run on a **different host** (the maintainer's workstation in a native venv, or
  another LAN machine) until prod carries the new library with `SO_REUSEADDR`/`SO_REUSEPORT`.
- Passive observation on the NAS uses AF_PACKET sniffing (`scripts/phase0/listen.py`).
- WP-1.3's listener MUST set both reuse flags so future dev/prod instances can coexist.

- [ ] Dev-container HA instance with the new build against the **live hub** (read-only-ish:
      observe state sync during normal household use for a day or two).
- [ ] Fault injection: kill/deafen the listener, block UDP, reboot the bridge — confirm
      reconciliation heals within one poll interval and availability flags correctly.
- [ ] Cut `hacs_rako` 0.7.0 release; upgrade prod HA via HACS.

## 5. Versioning & release summary

| Artefact | From | To | Notes |
|----------|------|----|-------|
| `python-rako-2025` (PyPI) | 0.4.1 | **0.5.0** | New state API is additive but behaviour of `set_*` changes (verify) — minor bump, changelog flags it |
| `hacs_rako` | 0.6.0 | **0.7.0** | Requires `python-rako-2025==0.5.0`; entity-name migration noted in release notes |
| HA minimum | 2025.5.0 | ≥ 2025.x current baseline | Set from what WP-2.1 actually uses |

## 6. Open questions for the maintainer

1. Phase 0 timing — when can we run characterisation against the live hub (it sends real
   commands to real lights)?
2. UDP vs HTTP as the default command transport once both verify properly — any preference
   from past reliability experience?
3. Entity naming migration (`has_entity_name`) renames entities in the UI; existing
   automations referencing entity_ids keep working (unique_ids unchanged), but dashboards
   showing names will change. Acceptable for 0.7.0?
4. Keep the room-level "light" (whole-room scene as a dimmable light) as-is, or also expose
   scenes as HA `scene`/`select` entities while modernising? (Out of scope unless wanted.)
5. Is the fan/ventilation percentage↔scene mapping behaving as you want today? If it's
   already right, WP-2.2 preserves it exactly.
