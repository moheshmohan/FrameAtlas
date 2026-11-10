"""Bounded native speech extraction for sources with large decoded audio tracks."""
from pathlib import Path
from tempfile import TemporaryDirectory
import wave

from .native import TICKS

DIRECT_BYTES = 384 * 1024 ** 2
WINDOW_BYTES = 64 * 1024 ** 2
MAX_WORDS = 200_000


def speech_absence(message):
    reason = message.strip().lower().rstrip(".")
    if reason == "no speech" or reason.endswith(": no speech"):
        return "no_speech"
    if any(reason == phrase or reason.endswith(": " + phrase) for phrase in (
        "no audio", "none of the clips has audio", "no decodable audio",
    )):
        return "no_audio"
    return None


def understand(native, items, model, language):
    return native.run("media.understandSpeech", {
        "items": items, "model": model, "language": language,
        "diarize": False, "faces": False, "wait": True,
    })


def transcribe_item(native, item, model, language):
    understand(native, [item], model, language)
    return native.run("transcript.inspect", {"item": item})


def native_id(value, excluded):
    if type(value) is not int or value <= 0 or value in excluded:
        raise RuntimeError("FilmCraft returned an invalid speech-window media ID")
    return value


def read_window(native, item, start, end, rate, channels, model, language):
    ids = []
    with TemporaryDirectory(prefix="speech-", dir=native.work) as folder:
        try:
            created = native.run("clip.makeSubclip", {
                "item": item, "start": start, "end": end, "name": "Speech window",
            })
            subclip = native_id(created.get("item"), (item,))
            ids.append(subclip)
            extracted = native.run("clip.extractAudio", {"items": [subclip], "dir": folder}).get("extracted", [])
            if len(extracted) != 1:
                raise RuntimeError("FilmCraft did not extract exactly one speech window")
            audio = native_id(extracted[0].get("item"), (item, subclip))
            ids.append(audio)
            path = Path(extracted[0]["path"])
            if path.is_symlink() or path.parent.resolve() != Path(folder).resolve() or not path.is_file():
                raise RuntimeError("FilmCraft returned a speech window outside its temporary folder")
            if path.stat().st_size > WINDOW_BYTES // 2 + 4096:
                raise RuntimeError("Extracted speech window exceeded its temporary byte allowance")
            expected_frames = end * rate // TICKS - start * rate // TICKS
            try:
                with wave.open(str(path), "rb") as pcm:
                    if (pcm.getframerate(), pcm.getnchannels(), pcm.getsampwidth(), pcm.getnframes()) != (
                        rate, channels, 2, expected_frames,
                    ):
                        raise RuntimeError("Extracted speech window did not match its requested source range")
            except (wave.Error, EOFError) as exc:
                raise RuntimeError("FilmCraft returned invalid speech-window audio") from exc
            try:
                return transcribe_item(native, audio, model, language)
            except RuntimeError as exc:
                if speech_absence(str(exc)):
                    return {}
                raise
        finally:
            if ids and not native.cancelled():
                native.run("project.select", {"items": ids})
                removed = native.run("project.delete", {"items": ids})
                if removed.get("items") != len(ids) or removed.get("offline", 0):
                    raise RuntimeError("FilmCraft could not release the temporary speech window")


def speech_window_parameters(probe):
    streams = probe.get("audio_streams", [])
    duration = probe.get("duration", 0)
    if streams and type(duration) is int and duration > 0:
        rate = streams[0].get("sample_rate")
        channels = streams[0].get("channels")
        if type(rate) is not int or not 0 < rate <= 384_000 or type(channels) is not int or not 0 < channels <= 64:
            raise ValueError("Invalid native audio dimensions")
        decoded_bytes = duration * max(rate, 16_000) * channels * 4 // TICKS
        if decoded_bytes > DIRECT_BYTES or duration > 10_800 * TICKS:
            return duration, max(rate, 8000), channels
    return None


def in_seconds(speech):
    for word in speech.get("words", []):
        word["start"] /= TICKS
        word["end"] /= TICKS
    speech["time_unit"] = "seconds"
    return speech


def transcribe(native, item, probe, model, language, stage):
    window = speech_window_parameters(probe)
    if window:
        return transcribe_windows(native, item, *window, model, language, stage)
    return in_seconds(transcribe_item(native, item, model, language))


def transcribe_windows(native, item, duration, rate, channels, model, language, stage):
    # Whole seconds usually align the cut with a source sample. Very wide audio needs
    # half-second windows to stay within the same decoded-byte bound.
    seconds = min(180, WINDOW_BYTES // (max(rate, 16_000) * channels * 4))
    window = seconds * TICKS if seconds else TICKS // 2
    count = (duration + window - 1) // window
    if not 1 <= count <= 1024:
        raise ValueError("Source exceeds the bounded speech window count. Split the source and retry.")
    words, languages, windows = [], set(), []
    for index, start in enumerate(range(0, duration, window), 1):
        if native.cancelled():
            raise RuntimeError("Job cancelled")
        end = min(start + window, duration)
        stage(f"Transcribing speech locally · section {index}/{count} · {model}")
        report = {}
        if end * rate // TICKS > start * rate // TICKS:
            report = read_window(native, item, start, end, rate, channels, model, language)
        found = report.get("words", [])
        if len(found) + len(words) > MAX_WORDS:
            raise ValueError("Source exceeds the transcript word limit. Split the source and retry.")
        for word in found:
            begin, finish = word.get("start"), word.get("end")
            if (type(begin) is not int or type(finish) is not int
                    or not 0 <= begin <= min(finish, end - start)
                    or finish > end - start + TICKS // rate):
                raise RuntimeError("FilmCraft returned word timing outside its speech window")
            words.append({**word, "start": (start + begin) / TICKS, "end": min(start + finish, end) / TICKS})
        if report.get("language"):
            languages.add(report["language"])
        windows.append({"start": start / TICKS, "end": end / TICKS, "words": len(found)})
    detected = next(iter(languages)) if len(languages) == 1 else ("mixed" if languages else "")
    return {"item": item, "language": detected, "source": "FilmCraft native speech windows",
            "text": " ".join(word["text"] for word in words), "words": words,
            "time_unit": "seconds", "windows": windows}
