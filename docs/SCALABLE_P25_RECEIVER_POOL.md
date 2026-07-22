# Scalable P25 Receiver Pool

The scanner now supports an automatically discovered RTL-SDR pool for OP25
`multi_rx.py`.

## Receiver selection

- The configured `receiver_roles.p25_control.rtl_serial` is ordered first and
  becomes the normal control-channel receiver.
- Every other connected RTL-SDR whose serial matches `0000025X` is added as a
  voice-capable receiver.
- Adding or removing a matching receiver does not require a code change.
- With fewer than two matching receivers, the wrapper automatically falls back
  to the validated legacy `rx.py` command.

OP25 `multi_rx.py` manages one tunable channel per RTL device. All channels are
assigned to the same trunking system. The first idle receiver is assigned to
control; the remaining idle receivers follow simultaneous allowed voice calls.

## Audio

Each pool receiver has a stable UDP audio port:

- `00000250` -> `23500`
- `00000251` -> `23501`
- ...
- `00000259` -> `23509`

`pi-p25-audio-pool.service` listens to all ten ports and forwards one active
source at a time to the existing browser-audio bridge on UDP `23456`. It never
mixes simultaneous PCM streams.

## Optional per-serial overrides

Create the ignored runtime file:

`runtime/settings/p25_receiver_overrides.json`

Example:

```json
{
  "receivers": {
    "00000252": {
      "enabled": true,
      "gain_db": 40,
      "ppm": 0
    },
    "00000253": {
      "enabled": true,
      "gains": "LNA:37",
      "ppm": -1
    }
  }
}
```

Receivers not listed use the validated marker defaults. The control receiver
cannot be disabled by an override.

## Safety and fallback

The original single-receiver application path is preserved in
`P25_VALIDATED_SINGLE_RX_APP`. The scalable wrapper falls back to it when:

- scalable mode is disabled;
- `multi_rx.py` is missing;
- the configured control serial is unavailable; or
- fewer than two enabled `0000025X` receivers are connected.
