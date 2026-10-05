# ESP32 firmware (planned)

No firmware yet. The ESP32 will drive the hardware the Jetson can't: LED ring and book spotlight, neck servos, the storage ring motor, the thermal printer, the base motors, and the pad's weight sensor. The Jetson side is `robot/drivers/esp32.py` (not implemented; `robot/drivers/fake.py` logs the same commands).

## Serial protocol

USB serial, 115200 baud, 8N1. One JSON object per line (UTF-8, `\n`-terminated), at most 512 bytes.

Jetson to ESP32, a command with an `id` the reply echoes:

```json
{"id": 7, "cmd": "light", "color": "#3a6fd8", "brightness": 0.2}
```

ESP32 to Jetson, the reply once the command has been applied (or rejected):

```json
{"id": 7, "ok": true}
{"id": 8, "ok": false, "error": "slot out of range"}
```

ESP32 to Jetson, unsolicited events (no `id`):

```json
{"event": "book_placed", "grams": 412}
{"event": "book_removed"}
{"event": "estop", "pressed": true}
```

### Commands

| cmd | parameters | sent by |
|---|---|---|
| `light` | `color` (`#rrggbb`), `brightness` (0..1) | `actuation/lights.py` |
| `spotlight` | `on` (bool) | `actuation/lights.py` |
| `neck` | `pose` (`lowered` or `upright`) | `actuation/neck.py` |
| `gaze` | `target` (`visitor`, `pad`, `forward`) | `actuation/neck.py` |
| `ring` | `slot` (0..7) | `actuation/storage_ring.py` |
| `print` | `text` (up to 500 chars) | `actuation/printer.py` |
| `speed_limit` | `mps` (metres per second) | `actuation/navigation.py` |
| `ping` | none | driver health check, replies `{"id": n, "ok": true}` |

The Jetson sends one command at a time and waits up to 1 s for the reply; three missed replies mark the link as down. The ESP32 stops the base motors if it hears nothing (not even `ping`) for 2 s.
