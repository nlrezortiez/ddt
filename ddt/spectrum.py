"""Hardware-independent decoding of the sweep CSV protocol."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Measurement:
    low_hz: int
    high_hz: int
    bin_hz: float
    powers_db: tuple[float, ...]


@dataclass(frozen=True)
class Detection:
    frequency_hz: float
    power_db: float


def decode(line: str) -> Measurement | None:
    """Accept both upstream CSV and this fork's optional range header.

    A frequency describes the bin centre, not a calibrated carrier frequency.
    The sample-count column describes the FFT, not the number of CSV powers.
    """
    fields = [field.strip() for field in line.split(",")]
    if fields[0].isdigit():
        try:
            ranges = int(fields[0])
            values = [int(value) for value in fields[1:]]
        except ValueError as exc:
            raise ValueError("invalid sweep header") from exc
        if 0 < ranges <= 10 and len(values) == ranges * 2 and all(
                0 <= lo < hi <= 7250 for lo, hi in zip(values[::2], values[1::2])):
            return None
        raise ValueError("incomplete sweep row")
    if len(fields) < 7:
        raise ValueError("incomplete sweep row")
    low, high = int(fields[2]), int(fields[3])
    width, samples = float(fields[4]), int(fields[5])
    powers = tuple(float(value) for value in fields[6:])
    if low < 0 or high <= low or not math.isfinite(width) or width <= 0:
        raise ValueError("invalid sweep frequencies")
    if samples <= 0 or not powers or any(math.isnan(p) or p == math.inf for p in powers):
        raise ValueError("invalid sweep powers")
    # Width is printed with two decimal places by sweep. Allow rounding error.
    if abs(len(powers) * width - (high - low)) > max(1, len(powers) * 0.01):
        raise ValueError("power count does not match frequency span")
    return Measurement(low, high, width, powers)


def detect(measurement: Measurement, threshold: float,
           excluded_hz: tuple[tuple[float, float], ...]) -> Detection | None:
    candidates = []
    for index, power in enumerate(measurement.powers_db):
        frequency = measurement.low_hz + (index + 0.5) * measurement.bin_hz
        if power > threshold and not any(lo <= frequency <= hi for lo, hi in excluded_hz):
            candidates.append(Detection(frequency, power))
    return max(candidates, key=lambda item: item.power_db, default=None)
