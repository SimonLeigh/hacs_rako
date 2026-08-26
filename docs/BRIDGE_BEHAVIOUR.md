# Rako Bridge Behaviour — Phase 0 Characterisation Findings

**Bridge under test:** a Rako WTC-Bridge (address/MAC withheld), firmware **2.5.0 WTC** (build Sep 26 2019), dbVersion -18.
**Installation:** 76 light entities (25 rooms), 2 ventilation entities, 50 room-channels in level cache.
**Method:** passive UDP listener on 9761 logging raw bytes + parses; cache queries;
cross-checked against official Rako protocol docs
([v2.2.2 "Accessing the Rako Bridge"](https://rakocontrols.com/media/1956/accessing-the-rako-bridge-v222.pdf),
[3rd-party access protocols](https://rakocontrols.com/media/1391/rako-bridge-3rd-party-access-protocols.pdf);
local copy `python-rako/doc/accessing-the-rako-bridge.pdf`).
**Status:** interactive experiments COMPLETE (2026-08-25): physical keypad, Rako app (scene +
slider), HA-originated commands, echo latency. Overnight soak complete (facts 17–22).

## Confirmed facts (doc + observation agree)

1. **Broadcasts are the only live level source.** Official doc, p.8: *"There is no current
   way of recalling the dimmer's level but using the level cache and scene cache together
   can produce a good approximation."* → any design must treat cache polling as an
   approximate backstop, never ground truth for levels.
2. **Fade buttons DELETE the room's scene-cache entry** (doc p.6). Observed: only 15 of 25
   rooms present in scene cache; several frequently-faded rooms absent. Consequences:
   - `scene_cache.get(room, 0)` in the current integration mis-initialises those rooms as "off".
   - Reconciliation-by-poll is structurally blind to fade-controlled and absent rooms.
3. **Keypad presses DO broadcast** — coverage from the wall keypads appears complete; the
   gaps are in the library's *decoder*, not the bridge's *transmitter*:
   - `FADE` (0x32/50, flags bit0 = direction, bit7 = default rate) and `STOP` (0x0F/15) are
     broadcast on press/release of up/down buttons. Current library drops both as
     `UnsupportedMessage`. **No final level is broadcast after a fade stops** — after any
     fade the true level is unknowable (see fact 1); best approximations: mark state
     unknown, or estimate from fade duration.
   - `SET_SCENE` (0x31/49) broadcast verified: data = [flags, scene]; library parse
     (`data[1]`) is **correct** (validated against doc example `0x530A00050031 0104...` =
     room 5 scene 4, and live observation of an "off" press → scene 0).
   - **Undocumented command 0x33 (51)** observed from keypad scene buttons mapped to another room (158): `[83, 8, 0, 158, 0, 51, 128, 255, 1, CRC]` then
     `[..., 128, 255, 0, CRC]`. Not in the official instruction table (jumps 0x32→0x34;
     doc: "not the full extent of commands"). Confirmed physically: first press turned room 158 lights ON (data[2]=1), second turned them OFF (data[2]=0) → working
     interpretation `[flags, level, on/off]`. Currently dropped as `UnsupportedMessage`.
4. **Legacy commands appear in feedback**: doc explicitly recommends monitoring SC1–SC4
   (0x03–0x06) and LEVEL_SET (0x0C) in status messages. Library handles these. STORE (0x0D)
   = "keypad finished saving a scene" — scene definitions can change at runtime; a level
   cache refresh should be triggered on STORE.
5. **HTTP command responses are weak acknowledgements** (doc p.2): "Success!" only means
   the bridge received the HTTP request, NOT that the circuit acted. UDP replies are
   "AOK"/"AERROR". Neither transport confirms the lamp changed.
6. **Scene cache 2-byte format is `scene<<10 | room`** (10-bit room). Library's parse
   (`scene = byte>>2`, room = next byte) is correct only for rooms ≤ 255; extended rooms
   (256–1019) would be mis-parsed. Same 10-bit caveat applies to level-cache records and
   status messages (`room = byte2*256 + byte3` is correct there).
7. Status messages come from the bridge's ephemeral source port (observed 2861), not 9761;
   the listener must bind 9761 and accept any source port from the bridge IP.
8. **App slider drags broadcast the TRUE level** as legacy `LEVEL_SET` (0x0C,
   data=[level, level]) — observed slider→~50% broadcast `brightness=129`. The doc's "no
   way to recall levels" applies only to querying; live listening captures real dimmer
   levels. App scene selections broadcast as legacy `SC1`–`SC4` (0x03–0x06), not 0x31 —
   both forms must be (and are) decoded.
9. **Scene selections repopulate the scene cache** (room 6 reappeared as scene 2 after an
   app scene select), but **channel-level changes do NOT update the cache**: after the
   slider drag, cache still said "room 6 scene 2" (ch1 defined level 26) while the true
   ch1 level was 129. ⚠️ Design-critical: naive poll-reconciliation would overwrite
   correct push-tracked levels with stale scene-derived ones. The coordinator must track
   per-channel provenance and only apply cache-derived levels when the cached *scene*
   differs from the tracked scene (i.e. a missed scene change), never on top of fresher
   LEVEL_SET tracking.
10. **No broadcast loss observed for real state changes** (all physical/app actions that
   visibly changed lights produced exactly one broadcast). One app tap produced no
   broadcast AND no light change — the command never reached the bridge (app/Wi-Fi issue),
   i.e. command loss upstream of the bridge, not broadcast loss. Post-command
   echo-verification would catch exactly this failure class.
11. **HA-originated UDP commands echo back as broadcasts** (SET_LEVEL 0x34 observed for
   both off→0 and on→255, flags=1). Post-command echo-verification is viable for all
   command sources.
12. **Command loss is real and observed twice in one evening** (an app scene tap and an
   HA on-toggle both produced no broadcast, no light change, no cache change — the
   command never took effect). Broadcast loss: still zero observed. The reliability
   problem is the command path, not the status path → echo-verify + retry on silence is
   the highest-value fix.
13. **`scenes.htm` HTTP endpoint works** and returns the live scene cache as hex pairs
   (`scene<<10 | room`), decoding identically to the UDP query — an HTTP reconciliation
   path that avoids UDP socket contention with the listener. (`levels.htm` does not exist.)
14. **Echo latency (self-sent UDP SET_LEVEL, 4 samples): send→echo 144–306 ms;
   send→AOK 677–770 ms.** The status broadcast arrives 3–5× BEFORE the bridge's own AOK
   reply. Echo-verification is both more truthful than the ack (see fact 5) and faster.
   Recommended verify window: 1.5 s (≈5× headroom), then retry once, then flag failure.
15. **Keypad presses are fully observable as events** (room + channel + command +
   direction for fades), including presses on buttons not mapped to any HA-tracked light
   (e.g. cmd 0x33 toggles, fade up/down). Requirement (maintainer): surface these as HA events
   / device triggers so automations can react to Rako keypads for non-Rako systems.
16. Keypad→room mapping is arbitrary (observed: one keypad's buttons mapped to its own room's fade AND another room's toggles). Never infer room from the keypad; trust only
   the room id in each status message.

## Divergence mechanisms (ranked, as verified so far)

| # | Mechanism | Verified? | Mitigation in new design |
|---|-----------|-----------|--------------------------|
| D1 | FADE/STOP broadcasts dropped by decoder; no post-fade level broadcast | ✅ observed | Decode FADE/STOP; mark channel state "estimated/unknown"; optional fade-duration estimation; assume-on at scene-1-level? (decide in design) |
| D2 | Undocumented cmd 0x33 dropped | ✅ observed | Decode empirically; log-and-refresh on any unknown command instead of silent drop |
| D3 | Scene cache missing fade-controlled rooms → wrong startup state | ✅ observed | Don't trust cache absence = off; initialise as unknown; use last-known persisted state |
| D4 | Listener task dies silently on error (integration) | ✅ code inspection | Supervised listener with backoff + health |
| D5 | Missed UDP broadcasts (loss) | plausible, not yet measured | Periodic reconcile poll (bounded staleness for scene-derived state only — see fact 1) |
| D6 | Optimistic update on unverified commands (UDP timeout=success; HTTP unchecked) | ✅ code inspection | Verify via echo broadcast; reconcile on silence |
| D7 | Scene definitions changed at runtime (STORE, app scene-store) | doc-supported | Refresh level cache on STORE broadcast |

## Raw observation log

### App experiment (room 6, 2026-08-25 ~20:14–20:16)

```
20:14:24.040 S room=6 ch=0 cmd=4  SC2_LEGACY → scene 2                       — parsed OK
             (prior scene-1 tap: no broadcast, no light change — tap lost app-side)
20:14:44.929 S room=6 ch=1 cmd=12 LEVEL_SET data=[129,129] → brightness 129  — parsed OK (slider ~50%)
20:16:37.981 S room=6 ch=0 cmd=3  SC1_LEGACY → scene 1                       — parsed OK (lights changed)
```

Scene cache after: room 6 = scene 2 (reappeared; slider change NOT reflected — see fact 9).

### HA experiment (room 7 ch 2, 2026-08-25 ~20:18–20:20)

```
20:18:41.786 S room=7 ch=2 cmd=52 SET_LEVEL data=[1,0]   → brightness 0   — HA "off" echo, parsed OK
             (HA "on" pressed moments later: NO echo, lights stayed off, HA showed off — command lost)
20:20:05.042 S room=7 ch=2 cmd=52 SET_LEVEL data=[1,255] → brightness 255 — HA "on" echo, parsed OK
```

### Keypad experiment (2026-08-25 ~20:06)

Room 9 keypad. Button mapping (per maintainer): "up" = room 9 lights on (fade),
"off" = room 9 off, scene buttons 1/2 toggle other rooms (158/159).

```
20:05:58.383 S room=9   ch=0 cmd=50 FADE  data=[128,0,0,0,0]  (fade up, default rate) — DROPPED by lib
20:05:58.690 S room=9   ch=0 cmd=15 STOP  (button release)                            — DROPPED by lib
20:06:18.349 S room=9   ch=0 cmd=49 SET_SCENE data=[1,0,...] → scene 0 (off press)    — parsed OK
20:06:22.650 S room=158 ch=0 cmd=51 ???   data=[128,255,1]                            — DROPPED by lib
20:06:24.801 S room=158 ch=0 cmd=51 ???   data=[128,255,0]                            — DROPPED by lib
20:06:26.336 S room=9   ch=0 cmd=50 FADE  data=[128,0,0,0,0]                          — DROPPED by lib
20:06:26.643 S room=9   ch=0 cmd=15 STOP                                              — DROPPED by lib
```

Scene cache before/after both showed room 9 = scene 0; room 158 absent from cache
throughout (never scene-set, only cmd-51/fade controlled).

## Overnight soak (2026-08-25 20:37 → 2026-08-26 19:57, 23.3 h, passive sniff on the HA host)

297 unique bridge packets (log carried ~50% two-NIC duplicates; deduped at <50 ms).

17. **Occupancy sensors dominate traffic and carry a distinct flag.** 240/267 SET_SCENE
    messages came from two rooms (145, 10) in bursts every 2–30 s while occupied
    (scene 1) ending with scene 0 on timeout. Their flags byte is **9 (0b1001)** whereas
    every keypad/app/HA scene set carries **1**. Confirmed: both rooms have Rako PIR sensors. **Flags bit 3 =
    sensor/automatic origin** (undocumented). Use it to tag event origin and to avoid
    treating PIR retriggers as keypad presses; consider suppressing redundant identical
    scene re-sends in the coordinator (240 full scene fan-outs/day today).
18. **The bridge itself emits duplicate broadcasts ~200 ms apart** for some keypad
    events (observed OFF ×2 at 197 ms, FADE ×2 at 195 ms and 240 ms) — distinct from
    multi-NIC capture duplicates (<1 ms). Echo-verify and event firing need a ~300 ms
    dedupe window keyed on (room, channel, command, data).
19. **Overnight: one event only** — 02:55:40, room 161 ch 1 SET_LEVEL 255, flags=1
    (origin: probably an HA automation — unconfirmed). No cloud or other scheduled traffic
    seen in 23 h.
20. **Other hosts send discovery `'D'` on 9761** (two LAN hosts, one on an adjacent subnet
    visible via the HA host's second NIC; one burst of six at 6 s intervals). Confirmed: two phones running the Rako app. The listener
    must ignore non-`S` packets from non-bridge sources (it does; the current library logs
    them as unsupported).
21. **Decoder coverage on real traffic: 96%** — the 4% dropped are exactly the
    FADE/STOP/`D` packets already identified. Nothing new and unparseable appeared.
22. **Cross-host consistency:** during the 6-minute overlap the workstation listener and
    the HA-host sniffer saw identical bridge packets. Still no observed broadcast loss.

Per-hour activity (unique packets): evening 16/16/4, quiet 22:00–09:00 (2), daytime peaks
of 92 (12h) and 50 (15h) — all sensor bursts.

## Open items

- [x] Confirm physical effect of the two cmd-0x33 events — YES: courtyard on → off.
      Hypothesis `data=[flags, level, on/off]` strengthened.
- [x] Rako app: scene select — broadcasts legacy SC1–SC4 (0x03–0x06), parsed OK
- [x] Rako app: channel slider — broadcasts LEVEL_SET (0x0C) with true level, parsed OK
- [ ] App scene-store: does it emit STORE (0x0D)? Does level cache update?
- [x] HA-originated UDP command: echoed as broadcast ✅ (off and on both observed)
- [x] Echo latency: 144–306 ms echo, 677–770 ms AOK (see fact 14)
- [ ] HA-originated HTTP command echo (prod HA uses UDP; test HTTP commander separately)
- [x] Dual listeners: prod HA and the dev Mac listener both received every broadcast all evening ✅
- [~] Bridge schedules / cloud-app: 23 h soak saw no cloud traffic; one 02:55 fan event of unknown origin (fact 19)
- [x] 23 h soak: no observed loss; sensor storms and bridge-level duplicates characterised (facts 17–22)
- [x] `scenes.htm` works as an HTTP scene-cache read (see fact 13)
- [ ] Decode cmd 0x33 fully (send it ourselves to a test channel with varying data bytes)
- [ ] Fade-duration → level estimation: measure default fade rate (fade up from 0 to full, time it)
