# Copper Launcher

A native Ubuntu desktop app, built with Python 3 and PySide6, that checks the machine and starts programs in order.

The default sequence is LM Studio, Ollama, Lemonade, YouTube Music, Brave, and Cursor. Anything already running is left alone. You can add, remove, and reorder programs from the Sequence control in the window.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python copper_launcher.py
```

Pass `--auto-start` to run the sequence as soon as the window opens.

## Dock and login

Edit the paths in `copper-launcher.desktop`, then:

```bash
mkdir -p ~/.local/share/applications
cp copper-launcher.desktop ~/.local/share/applications/
```

For login, edit `copper-launcher-autostart.desktop` the same way and copy it to `~/.config/autostart/`.

Ports, executable overrides, and the built-in program list are at the top of `copper_launcher.py`. Edits you make in the Sequence window are saved to `~/.config/copper-launcher/sequence.json`.

Inter and JetBrains Mono are included under the SIL Open Font License. See `fonts/OFL-Inter.txt` and `fonts/OFL-JetBrainsMono.txt`.
