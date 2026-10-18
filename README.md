# ALS MIDI Remapper

Edits the MIDI mappings in an Ableton Live set (`.als`). Live only maps controls by MIDI learn, so this opens a set, shows each mapping in a table, and saves a new `.als` with your changes. It runs on Windows and macOS and targets Live 12 sets.

This is experimental. Open the saved set in Live and confirm the mappings before you rely on it.

## Install

```
pip install -r requirements.txt
```

## Run

Windows:

```
py -3 main.py
```

macOS:

```
python3 main.py
```

Pass a set path to open it immediately, for example `py -3 main.py "My Set.als"`.
