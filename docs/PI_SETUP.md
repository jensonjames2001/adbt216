# Running it on a Raspberry Pi 3B

A Pi 3B is enough. The service makes about fifty small web requests an hour and
uses well under 100 MB of memory. Checked on 2 October 2026: the project installs
from ready-made packages (no compiling) and passes all 238 tests on Python 3.13,
which is what the current Raspberry Pi OS ships. The current **Raspberry Pi OS
Lite (64-bit)**, Debian 13 "trixie" released 15 September 2026, lists the 3B as
supported. Use the 64-bit Lite image, not the 32-bit one and not the desktop one.

## You need

- The Pi 3B, its official power supply (2.5 A) and a micro-SD card of 16 GB or
  more. A card marked A1 or A2 lasts longer under constant small writes.
- An Ethernet cable to the router if at all possible. The 3B's Wi-Fi is 2.4 GHz
  only and drops out more than a cable does.
- A Windows, Mac or Linux computer for about ten minutes to write the SD card.
  An iPad cannot write SD cards. After that, the iPad is fine for everything
  else through an SSH app such as Termius or a-Shell, both free.

## 1. Write the SD card (once)

1. Install Raspberry Pi Imager from raspberrypi.com/software on the computer.
2. Device: Raspberry Pi 3. Operating system: Raspberry Pi OS (other) →
   Raspberry Pi OS Lite (64-bit). Storage: the SD card.
3. When it asks to apply OS customisation, say yes and set: hostname `rgalerts`,
   a username and password you will remember, Wi-Fi details only if you cannot use
   a cable, locale time zone `Europe/London`, and under Services enable SSH with
   password authentication.
4. Write the card, put it in the Pi, connect the cable and power. Wait two minutes.

## 2. Log in and install

From the iPad's SSH app or any computer on the same network:

    ssh YOURUSER@rgalerts.local

Then, one line at a time:

    sudo apt update && sudo apt full-upgrade -y
    sudo apt install -y git python3-venv
    git clone -b phase-0-probes https://github.com/jensonjames2001/adbt216.git rgalerts
    cd rgalerts
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e ".[dev]"
    .venv/bin/python -m pytest -q

The last line must end with `238 passed`. The repository is public today, so the
clone needs no login. If it ever asks for a password, the repository has been made
private: on GitHub create a personal access token (Settings → Developer settings →
Personal access tokens, repo scope) and paste that as the password.

## 3. Keys

    cp .env.example .env
    nano .env

Fill in the values, then Ctrl+O, Enter, Ctrl+X to save. `.env` stays on the Pi
and is never printed or saved by anything in this project.

## 4. Run the probes, in this order

    .venv/bin/python probes/probe_telegram.py            # prints the chat id; put it in .env
    .venv/bin/python probes/probe_telegram.py --send     # a sample alert appears in the chat
    .venv/bin/python probes/probe_waze.py                # "blocked from this machine" or not: the key Waze question
    .venv/bin/python probes/probe_nh.py
    .venv/bin/python probes/probe_tomtom.py              # one sample, 46 requests

For the six-hour TomTom sample, start it so it survives closing the SSH app:

    nohup .venv/bin/python probes/probe_tomtom.py --repeat 6 --interval 3600 --max-requests 276 > tomtom_run.log 2>&1 &

Check on it later with `tail tomtom_run.log`.

## 5. Send the findings back

Every run writes `findings.md` under `fixtures/<source>/live/<timestamp>/`. They
contain no keys. Paste the output of this into the chat:

    cat fixtures/*/live/*/findings.md

## Later

Phase 4 adds a `systemd` unit so the service starts at boot and restarts itself,
and the README will cover updating, stopping and changing the coverage area. Keep
the Pi plugged in, on the cable, and leave it alone.
