"""Original synthesized alarm sounds, packaged as short PCM WAV clips."""

import io
import math
import struct
import wave
from dataclasses import dataclass


@dataclass(frozen=True)
class Sound:
    # Each note has a starting frequency, ending frequency and duration. Zero is silence.
    notes: tuple[tuple[int, int, float], ...]

    @property
    def duration(self) -> float:
        return sum(note[2] for note in self.notes)


SOUNDS = {
    "chime": Sound(((523, 523, 0.13), (659, 659, 0.13), (784, 784, 0.22))),
    "beeps": Sound(((1120, 1120, 0.10), (0, 0, 0.08), (1120, 1120, 0.16))),
    "red-alert": Sound(((420, 790, 0.21), (0, 0, 0.07), (420, 790, 0.26))),
}


def render_wav(sound: Sound) -> bytes:
    rate = 24_000
    samples = bytearray()
    for start, end, duration in sound.notes:
        count = round(duration * rate)
        for index in range(count):
            seconds = index / rate
            phase = math.tau * (start * seconds + (end - start) * seconds**2 / (2 * duration))
            edge = min(1, index / (rate * 0.012), (count - index - 1) / (rate * 0.025))
            samples.extend(struct.pack("<h", round(12_000 * edge * math.sin(phase))))
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(samples)
    return out.getvalue()
