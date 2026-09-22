# mqtt-supa bridge

Disclaimer: All trademarks belong to their respective owners.

Forward MQTT messages to **Supabase**. If Supabase is unreachable, failed messages are saved locally to **SQLite** and can be replayed later with `retry_failed.py`.

Designed for low-resource ARM devices (Orange Pi, Raspberry Pi Zero, etc.). Installation instructions assume **Armbian** or a similar Debian-based Linux distribution.

## Use cases

**General:** You have a working Mosquitto broker and need it to forward messages to Supabase.

**Domotics:** You have an old Orange Pi or any 32-bit ARM device running Zigbee2MQTT and Mosquitto. Current versions of Home Assistant no longer support this architecture or supervised/core installs. This bridge lets your device keep doing what it does best — running the Zigbee stack — while sending data to Supabase, where Home Assistant or any other frontend can read it.

## Testing

This has been tested in an Orange Pi device for days and sent thousands of messages.

## Requirements

- Python 3.9+
- Mosquitto (or any MQTT broker)
- A Supabase project

## How it works

Incoming MQTT messages are held in a small RAM buffer rather than sent one by one. The buffer is flushed to Supabase either when it reaches `BUFFER_SIZE` messages or after `FLUSH_INTERVAL` seconds — whichever comes first. This reduces the number of HTTP calls and is gentler on both the device and Supabase.

Certain events are classified as **urgent** (occupancy, open door/window, tamper, water leak). By default these are prepended to the front of the buffer and sent on the next flush ahead of normal messages. On high-throughput setups you can set `URGENT_SKIP_BUFFER=true` to send them immediately on their own thread, bypassing the buffer entirely.

If a flush fails because Supabase is unreachable, the messages are written to a local SQLite file (`failed_messages.db`) instead of being lost. Run `retry_failed.py` manually once connectivity is restored to replay them.

## Supabase setup

Run this once in the Supabase SQL editor before starting the bridge:

```sql
CREATE TABLE mqtt_messages (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "received_at(UTC)" TIMESTAMPTZ NOT NULL,
    topic              TEXT        NOT NULL,
    payload            JSONB       NOT NULL
);

CREATE INDEX mqtt_messages_topic_idx       ON mqtt_messages (topic);
CREATE INDEX mqtt_messages_received_at_idx ON mqtt_messages ("received_at(UTC)");

ALTER TABLE public.mqtt_messages ENABLE ROW LEVEL SECURITY;
```

## Installation

1. Create a folder and a virtual environment:

```bash
cd ~
mkdir mqtt-supa
cd mqtt-supa
python3 -m venv .venv
```

2. Place `mqtt_supa.py`, `retry_failed.py`, `requirements.txt`, and `.env` in that folder.

3. Install requirements:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

4. Create a dedicated Mosquitto user for the bridge:

```bash
sudo mosquitto_passwd /etc/mosquitto/passwd mqtt-supa
```

## Configuration

Copy `.env.example` to `.env` and fill in your values (see .env.example for details):

```bash
cp .env.example .env
nano .env
```

The following can also be adjusted directly in `mqtt_supa.py`:

```python
EXCLUDED_TOPICS = ["z2m/bridge/logging"]  # topics to ignore entirely
```

The is_urgent function can also be modified. By default, it analyzes the payload and: returns true if "occupancy" is present and true, or if "contant" is present and false, or if "tamper" is present and true, or if "water_leak" is present and true.


## Testing

Activate the virtual environment and run directly to verify everything works before installing as a service:

```bash
source .venv/bin/activate
python mqtt_supa.py
```

## systemd service

Once everything works, install it as a service:

```bash
sudo nano /etc/systemd/system/mqtt-supa.service
```

```ini
[Unit]
Description=MQTT to Supabase bridge
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/home/your_user/mqtt-supa
ExecStart=/home/your_user/mqtt-supa/.venv/bin/python /home/your_user/mqtt-supa/mqtt_supa.py
EnvironmentFile=/home/your_user/mqtt-supa/.env
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable mqtt-supa
sudo systemctl start mqtt-supa
```

## Logs

View past logs:
```bash
sudo journalctl -u mqtt-supa
```

Follow live:
```bash
sudo journalctl -u mqtt-supa -f
```

Past 100 logs plus follow:
```bash
sudo journalctl -u mqtt-supa -f -n 100
```

## Failed messages

When Supabase is unreachable, messages are saved to `failed_messages.db`.
Once connectivity is restored, replay them:

```bash
source .venv/bin/activate
python retry_failed.py
```

Rows are deleted from `failed_messages.db` as they are successfully sent.
If a batch fails the script stops and leaves remaining rows untouched —
run it again once the issue is resolved.

## Donations

If this helped you, consider donating.

ETH: 0xD0dB369549202992225CcB24aD6d82a80ae9804c