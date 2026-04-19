# How to set up a node

## 1. Requirements

You'll need a Linux machine.

Ask Bruno for an API key and endpoint (you'll put them in the `.env`).

Install `python`, `git`, `gcc`, `gfortran`, `cmake`, and `valgrind`.

## 2. Repo setup

Clone this repo and set up the environment with `python -m venv venv`.

Activate the environment with `. venv/bin/activate`, so you can install the dependencies with `pip install .`.

Next you'll need to edit your `.env`. Two of these you'll get from Bruno:

  - `NODE_API_ENDPOINT`: the endpoint of where the bot API is running.
  - `NODE_API_KEY`: this will uniquely identify your node.

The other three are up to you.

  - `NODE_MAX_THREADS`: how many simultaneous jobs or build threads can run. It's a cap on how much of your CPU can be used.
  - `NODE_BUILD_CACHE_DIR` and `NODE_WORK_BASE_DIR`: don't put them in /tmp/, since that's usually a tmpfs. I'd recommend creating a `dist` directory inside the repo itself and use `/path/to/repo/dist/cache` and `/path/to/repo/dist/work`. Keeps everything in the same directory, and `dist` is ignored by Git.

Once the `.env` has all that, you can test if your node runs by running `python -m deckbot node` (ensure the Python environment is activated).

## 3. `systemd` service

Of course, no one wants to start it manually every time. So we'll make it into a `systemd` user unit.

First, enable linger with `sudo loginctl enable-linger <username>` and ensure your daemon is up with `systemctl --user daemon-reload`.

Now, create a file `~/.config/systemd/user/deckbot-node.service` (create the directories if they do not exist) and put the following in:

```
[Unit]
Description=DeckBot Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/git/deckbot
EnvironmentFile=%h/git/deckbot/.env
ExecStart=%h/git/deckbot/venv/bin/python -m deckbot node
Restart=on-failure
RestartSec=30
TimeoutStopSec=300
KillMode=process

[Install]
WantedBy=default.target
```

Of course, you might want to change the `.../git/...` parts to point to wherever this repository actually is.

Once that file exists, you can start and enable it (so it runs automatically) with `systemctl --user enable --now deckbot-node`.

Check that it runs with `systemctl --user status deckbot-node`.

You can see the real-time logs of the node with `journalctl --user -u deckbot-node -f`.

## Done!

There, now you're contributing compute power for MYSTRAN tests and runs coordinated via DeckBot in the MYSTRAN Discord server.
