"""MIDI Map entries stored as KeyMidi elements inside a Live set."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from alsio import (
    MAP_TAGS,
    AlsError,
    load_als,
    mapping_spans,
    replace_attr,
    save_als,
    sibling_range_span,
)

TRACK_TAGS = {
    "AudioTrack",
    "MidiTrack",
    "GroupTrack",
    "ReturnTrack",
    "MasterTrack",
    "MainTrack",
    "PreHearTrack",
}
DEVICE_ALIASES = {
    "StereoGain": "Utility",
    "InstrumentGroupDevice": "Instrument Rack",
    "AudioEffectGroupDevice": "Audio Effect Rack",
    "MidiEffectGroupDevice": "MIDI Effect Rack",
    "MxDeviceMidiEffect": "Max MIDI Effect",
    "MxDeviceAudioEffect": "Max Audio Effect",
    "MxDeviceInstrument": "Max Instrument",
}
PARAMETER_ALIASES = {"Speaker": "Track Activator"}
CONTROL_LABELS = {
    "KeyMidiSceneUp": "Scene Up",
    "KeyMidiSceneDown": "Scene Down",
    "KeyMidiFireSelectedScene": "Scene Launch",
    "KeyMidiScrollSelectedScene": "Scroll Scene",
    "RandomizeMacrosKeyMidi": "Randomize Macros",
    "RecallSelectedSnapshotKeyMidi": "Recall Variation",
    "SnapshotIndexScrollerKeyMidi": "Select Variation",
}
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


@dataclass
class Mapping:
    """One MIDI mapping. Channel is 1-16 here; the file stores 0-15."""

    index: int
    track: str
    device: str
    parameter: str
    is_note: bool
    channel: int
    number: int
    map_mode: int
    range_min: str | None
    range_max: str | None
    range_unit: str
    orig_is_note: bool
    orig_channel: int
    orig_number: int
    orig_map_mode: int
    orig_range_min: str | None
    orig_range_max: str | None

    @property
    def is_dirty(self) -> bool:
        return (
            self.is_note != self.orig_is_note
            or self.channel != self.orig_channel
            or self.number != self.orig_number
            or self.map_mode != self.orig_map_mode
            or self.range_min != self.orig_range_min
            or self.range_max != self.orig_range_max
        )

    def capture_original(self) -> None:
        self.orig_is_note = self.is_note
        self.orig_channel = self.channel
        self.orig_number = self.number
        self.orig_map_mode = self.map_mode
        self.orig_range_min = self.range_min
        self.orig_range_max = self.range_max


@dataclass
class LiveSet:
    xml: str
    creator: str
    mappings: list[Mapping]

    @classmethod
    def load(cls, path: Path) -> LiveSet:
        return cls.from_xml(load_als(path))

    @classmethod
    def from_xml(cls, xml_text: str) -> LiveSet:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise AlsError(f"This file is not Ableton XML ({exc}).") from exc
        mappings = extract_mappings(root)
        spans = mapping_spans(xml_text)
        map_count = sum(1 for element in root.iter() if local_name(element.tag) in MAP_TAGS)
        if len(spans) != map_count:
            raise AlsError("Could not line up MIDI mappings in this Live set.")
        return cls(xml=xml_text, creator=root.attrib.get("Creator", ""), mappings=mappings)

    @property
    def is_dirty(self) -> bool:
        return any(mapping.is_dirty for mapping in self.mappings)

    def render(self) -> str:
        spans = mapping_spans(self.xml)
        if any(mapping.index < 0 or mapping.index >= len(spans) for mapping in self.mappings):
            raise AlsError("The Live set changed shape and can no longer be saved safely.")
        replacements: list[tuple[int, int, str]] = []
        for mapping in self.mappings:
            start, end = spans[mapping.index]
            block = self.xml[start:end]
            updated = block
            if mapping.is_note != mapping.orig_is_note:
                updated = replace_attr(updated, "IsNote", "true" if mapping.is_note else "false")
            if mapping.channel != mapping.orig_channel:
                updated = replace_attr(updated, "Channel", str(mapping.channel - 1))
            if mapping.number != mapping.orig_number:
                updated = replace_attr(updated, "NoteOrController", str(mapping.number))
            if mapping.map_mode != mapping.orig_map_mode:
                updated = replace_attr(updated, "ControllerMapMode", str(mapping.map_mode))
            if updated != block:
                replacements.append((start, end, updated))
            if mapping.range_min == mapping.orig_range_min and mapping.range_max == mapping.orig_range_max:
                continue
            if mapping.orig_range_min is None or mapping.orig_range_max is None:
                raise AlsError(f"{mapping.parameter} has no MIDI range to edit.")
            if mapping.range_min is None or mapping.range_max is None:
                raise AlsError(f"{mapping.parameter} needs both a minimum and a maximum.")
            range_span = sibling_range_span(self.xml, end)
            if range_span is None:
                raise AlsError(f"Could not find the MIDI range for {mapping.parameter}.")
            range_block = self.xml[range_span[0] : range_span[1]]
            if mapping.range_min != mapping.orig_range_min:
                range_block = replace_attr(range_block, "Min", mapping.range_min)
            if mapping.range_max != mapping.orig_range_max:
                range_block = replace_attr(range_block, "Max", mapping.range_max)
            replacements.append((range_span[0], range_span[1], range_block))
        if not replacements:
            return self.xml
        rendered = self.xml
        for start, end, text in sorted(replacements, key=lambda item: item[0], reverse=True):
            rendered = rendered[:start] + text + rendered[end:]
        return rendered

    def save(self, path: Path) -> None:
        rendered = self.render()
        save_als(path, rendered)
        self.xml = rendered
        for mapping in self.mappings:
            mapping.capture_original()


def extract_mappings(root: ET.Element) -> list[Mapping]:
    parent = {child: element for element in root.iter() for child in list(element)}
    mappings: list[Mapping] = []
    ordinal = 0
    for element in root.iter():
        map_tag = local_name(element.tag)
        if map_tag not in MAP_TAGS:
            continue
        index = ordinal
        ordinal += 1
        host = parent.get(element)
        if host is None:
            continue
        if map_tag == "KeyMidi":
            parameter = parameter_label(host, local_name(host.tag), parent)
            context_start: ET.Element | None = host
        else:
            parameter = control_label(map_tag, host)
            context_start = element
        device_labels, device_tags, track = mapping_context(context_start, parent)
        range_min, range_max = controller_range(host)
        is_note = child_value(element, "IsNote") == "true"
        try:
            stored_channel = int(required_value(element, "Channel"))
            number = int(required_value(element, "NoteOrController"))
            map_mode = int(child_value(element, "ControllerMapMode") or "0")
        except ValueError as exc:
            raise AlsError("A MIDI mapping has a channel or number that is not an integer.") from exc
        # Rack macro links are KeyMidi entries on channel value 16. That is outside
        # MIDI channels 1-16, so they are not controller mappings.
        if not 0 <= stored_channel <= 15:
            continue
        channel = stored_channel + 1
        unit = range_unit(local_name(host.tag) if map_tag == "KeyMidi" else "", device_tags, range_min is not None)
        mappings.append(
            Mapping(
                index=index,
                track=track,
                device=" / ".join(device_labels),
                parameter=parameter,
                is_note=is_note,
                channel=channel,
                number=number,
                map_mode=map_mode,
                range_min=range_min,
                range_max=range_max,
                range_unit=unit,
                orig_is_note=is_note,
                orig_channel=channel,
                orig_number=number,
                orig_map_mode=map_mode,
                orig_range_min=range_min,
                orig_range_max=range_max,
            )
        )
    return mappings


# Live.MidiMap.MapMode. "Smooth" is Live's linear (one step per click) variant.
MAP_MODES = (
    (0, "Absolute"),
    (5, "Absolute 14-bit"),
    (1, "Relative Signed Bit (accelerated)"),
    (6, "Relative Signed Bit (linear)"),
    (4, "Relative Signed Bit 2 (accelerated)"),
    (9, "Relative Signed Bit 2 (linear)"),
    (2, "Relative Bin Offset (accelerated)"),
    (7, "Relative Bin Offset (linear)"),
    (3, "Relative Two's Complement (accelerated)"),
    (8, "Relative Two's Complement (linear)"),
)


def map_mode_label(mode: int) -> str:
    for value, label in MAP_MODES:
        if value == mode:
            return label
    return f"Mode {mode}"


def note_name(number: int) -> str:
    """Ableton calls MIDI note 60 C3."""
    octave = number // 12 - 2
    return f"{NOTE_NAMES[number % 12]}{octave}"


def format_db(raw: str) -> str:
    amplitude = float(raw)
    if amplitude <= 0:
        return "-inf dB"
    return f"{20 * math.log10(amplitude):.2f} dB"


def db_from_amplitude(raw: str) -> float:
    amplitude = float(raw)
    if amplitude <= 0:
        raise ValueError("Amplitude must be positive to convert to dB.")
    return 20 * math.log10(amplitude)


def amplitude_from_db(db: float) -> str:
    return format_float(10 ** (db / 20))


def format_float(value: float) -> str:
    if value == 0:
        return "0"
    return f"{value:.10f}".rstrip("0").rstrip(".")


def _is_negative_infinity(text: str) -> bool:
    cleaned = text.strip().casefold().replace("db", "").strip()
    return cleaned in {"-inf", "-infinity"}


def parse_db(text: str) -> float:
    cleaned = text.strip().casefold().replace("db", "").strip().replace(",", ".")
    if not cleaned:
        raise ValueError("Enter a level in dB.")
    return float(cleaned)


def commit_range_text(mapping: Mapping, which: str, text: str) -> str:
    """Turn a table edit into the raw Value string Live stores."""
    current = mapping.range_min if which == "min" else mapping.range_max
    if current is None or mapping.range_unit == "none":
        raise ValueError("This control has no MIDI range.")
    if mapping.range_unit == "db":
        if _is_negative_infinity(text):
            return current if float(current) == 0 else "0"
        new_db = parse_db(text)
        if float(current) > 0 and round(new_db, 2) == round(db_from_amplitude(current), 2):
            return current
        return amplitude_from_db(new_db)
    value = float(text.strip().replace(",", "."))
    if float(current) == value:
        return current
    return format_float(value)


def range_display(mapping: Mapping, which: str) -> str:
    raw = mapping.range_min if which == "min" else mapping.range_max
    if raw is None:
        return ""
    if mapping.range_unit == "db":
        return format_db(raw)
    return raw


def number_display(mapping: Mapping) -> str:
    if mapping.is_note:
        return f"{mapping.number} ({note_name(mapping.number)})"
    return str(mapping.number)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_value(element: ET.Element, tag: str) -> str:
    for child in list(element):
        if local_name(child.tag) == tag:
            return child.attrib.get("Value", "")
    return ""


def required_value(element: ET.Element, tag: str) -> str:
    value = child_value(element, tag)
    if value == "":
        raise AlsError(f"A MIDI mapping is missing <{tag}>.")
    return value


def effective_name(element: ET.Element) -> str:
    for child in list(element):
        if local_name(child.tag) == "EffectiveName" and child.attrib.get("Value"):
            return child.attrib["Value"]
        if local_name(child.tag) == "Name":
            named = child_value(child, "EffectiveName")
            if named:
                return named
    return ""


def mapping_context(
    start: ET.Element, parent: dict[ET.Element, ET.Element]
) -> tuple[list[str], list[str], str]:
    device_labels: list[str] = []
    device_tags: list[str] = []
    track = ""
    current: ET.Element | None = start
    while current is not None:
        ancestor = parent.get(current)
        if ancestor is not None and local_name(ancestor.tag) == "Devices":
            device_labels.append(device_label(current))
            device_tags.append(local_name(current.tag))
        if not track and local_name(current.tag) in TRACK_TAGS:
            track = effective_name(current) or local_name(current.tag)
        current = ancestor
    device_labels.reverse()
    device_tags.reverse()
    return device_labels, device_tags, track


def parameter_label(host: ET.Element, parameter_tag: str, parent: dict[ET.Element, ET.Element]) -> str:
    owner = parent.get(host)
    if parameter_tag == "Timeable" and owner is not None and local_name(owner.tag).startswith("MxD"):
        name = child_value(owner, "Name").strip() or child_value(owner, "ShortName").strip()
        if name:
            return name
    if parameter_tag == "ParameterValue" and owner is not None:
        name = child_value(owner, "ParameterName").strip()
        if name:
            return name
    if parameter_tag.startswith("MacroControls.") and owner is not None:
        slot = parameter_tag.split(".", 1)[1]
        name = child_value(owner, f"MacroDisplayNames.{slot}").strip()
        if name:
            return name
    return PARAMETER_ALIASES.get(parameter_tag, parameter_tag)


def control_label(map_tag: str, host: ET.Element) -> str:
    if map_tag == "RecallSnapshotKeyMidi":
        name = child_value(host, "SnapshotName").strip()
        if not name:
            return "Variation"
        if name.casefold().startswith("variation"):
            return name
        return f"Variation {name}"
    return CONTROL_LABELS.get(map_tag, map_tag)


def device_label(element: ET.Element) -> str:
    user_name = child_value(element, "UserName").strip()
    if user_name:
        return user_name
    tag = local_name(element.tag)
    if tag.startswith("MxDevice"):
        patch = max_patch_name(element)
        if patch:
            return patch
    return DEVICE_ALIASES.get(tag, tag)


def max_patch_name(element: ET.Element) -> str:
    """File name of a Max for Live device, without the .amxd suffix."""
    for child in list(element):
        if local_name(child.tag) != "PatchSlot":
            continue
        for node in child.iter():
            if local_name(node.tag) not in {"RelativePath", "Path"}:
                continue
            value = node.attrib.get("Value", "")
            if value.lower().endswith(".amxd"):
                return Path(value).stem
    return ""


def controller_range(element: ET.Element) -> tuple[str | None, str | None]:
    for child in list(element):
        if local_name(child.tag) != "MidiControllerRange":
            continue
        minimum = child_value(child, "Min")
        maximum = child_value(child, "Max")
        if minimum == "" or maximum == "":
            return None, None
        return minimum, maximum
    return None, None


def range_unit(parameter_tag: str, device_tags: list[str], has_range: bool) -> str:
    if not has_range:
        return "none"
    if parameter_tag == "Volume" and not device_tags:
        return "db"
    if parameter_tag == "Gain" and device_tags[-1:] == ["StereoGain"]:
        return "db"
    return "raw"
