# Serial protocol

115200 baud, 8N1, newline-terminated ASCII, case insensitive. Every command
gets exactly one line in reply, so the host can treat it as request/response.

## Host to board

| Command | Reply | Effect |
|---------|-------|--------|
| `STAT <state>` | `OK <state>` | set the status; `<state>` is `FREE`, `UPCOMING`, `MEETING`, `BUSY` or `OFF` |
| `PING` | `PONG` | liveness check; also resets the stale timer |
| `BRIGHT <0-255>` | `OK BRIGHT <n>` | master brightness, applied to every colour |
| `RGB <r> <g> <b>` | `OK RGB` | manual colour, held until the next `STAT` |
| `?` or `STATE` | `STATE <name>` | report the current state |

Unknown input gets `ERR unknown command`; a bad state name gets
`ERR unknown state`.

`STAT STALE` is rejected. `STALE` is an internal state the board enters on its
own and is not settable.

## Board to host

On reset the board runs a red/green/blue self test, prints

```
READY meeting_status_led
```

and enters the stale state (breathing dim blue) until the host speaks. That
banner is the one line it sends unprompted; everything else it emits is a reply
to a command.

Because opening the port resets the board, the host sees this banner shortly
after connecting — it is a useful confirmation that the sketch, and not some
other firmware, is on the board.

## The stale timer

If the board hears nothing for `STALE_MS` (20 seconds), it switches to the
stale state and clears any manual `RGB` colour. This is what makes an unplugged
or crashed host visible rather than leaving a stale green light saying you are
free.

The host resends the current state every `heartbeat` seconds (5 by default) to
hold it off. **Any value of `heartbeat` must stay well under 20 seconds**, or
the LED will flick to blue between updates.

## Talking to the board by hand

```sh
.venv/bin/python -c "
import serial, time
s = serial.Serial('/dev/cu.usbserial-1340', 115200, timeout=1)
time.sleep(2)                      # the board resets when the port opens
s.write(b'STAT MEETING\n'); print(s.readline())
"
```

The 2-second wait matters: opening the port toggles DTR, which resets the
board, and the bootloader will eat anything sent before it finishes.

## Wire format notes

- The host writes `STAT %s\n` with `write_timeout` set and **never** calls
  `flush()`. `flush()` maps to `tcdrain()`, which blocks indefinitely if the
  board stops reading; the write timeout is the safe equivalent.
- The board parses with `strtok` into a 48-byte line buffer. Longer lines are
  truncated, not overflowed.
