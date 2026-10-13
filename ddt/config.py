from dataclasses import dataclass, fields
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class Config:
    sweep_binary: str = "build/bin/sweep"
    serial: str = ""
    ranges_mhz: tuple[tuple[int, int], ...] = ((0, 6000),)
    excluded_mhz: tuple[tuple[float, float], ...] = ((0, 100), (2000, 2700), (5000, 5400))
    threshold_db: float = -32.2
    lna_gain: int = 32
    vga_gain: int = 20
    bin_width_hz: int = 1000000
    startup_timeout: float = 15.0
    data_timeout: float = 5.0
    retry_delay: float = 2.0
    retry_max_delay: float = 30.0
    stop_timeout: float = 3.0
    alert_hold: float = 3.0
    display_enabled: bool = True
    gpio_enabled: bool = True
    i2c_port: int = 1
    i2c_address: int = 60
    gpio_chip: int = 0
    status_file: str = "/run/ddt/status.json"

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        values = json.loads(Path(path).read_text()) if path else {}
        if not isinstance(values, dict):
            raise ValueError("configuration must be a JSON object")
        unknown = values.keys() - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"unknown configuration keys: {sorted(unknown)}")
        for key in ("ranges_mhz", "excluded_mhz"):
            if key in values:
                values[key] = tuple(tuple(pair) for pair in values[key])
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        for name in ("display_enabled", "gpio_enabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        for name in ("startup_timeout", "data_timeout", "retry_delay", "retry_max_delay",
                     "stop_timeout", "alert_hold"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if type(self.threshold_db) not in (int, float) or not math.isfinite(self.threshold_db):
            raise ValueError("threshold_db must be finite")
        for name in ("lna_gain", "vga_gain", "bin_width_hz", "i2c_port", "i2c_address", "gpio_chip"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be integer")
        if self.lna_gain not in range(0, 41, 8) or self.vga_gain not in range(0, 63, 2):
            raise ValueError("invalid HackRF gains")
        if not 2445 <= self.bin_width_hz <= 5000000:
            raise ValueError("invalid bin width")
        if self.i2c_port < 0 or self.gpio_chip < 0 or not 0x03 <= self.i2c_address <= 0x77:
            raise ValueError("invalid hardware address")
        if not self.ranges_mhz or len(self.ranges_mhz) > 10:
            raise ValueError("provide 1..10 sweep ranges")
        for pair in self.ranges_mhz:
            if len(pair) != 2 or any(type(v) is not int for v in pair) or not 0 <= pair[0] < pair[1] <= 7250:
                raise ValueError("invalid sweep range")
        for pair in self.excluded_mhz:
            if len(pair) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in pair) or not 0 <= pair[0] <= pair[1]:
                raise ValueError("invalid excluded range")
        if self.retry_max_delay < self.retry_delay:
            raise ValueError("retry_max_delay must be >= retry_delay")
        if not isinstance(self.serial, str) or not isinstance(self.sweep_binary, str) or not self.sweep_binary:
            raise ValueError("invalid receiver identity")
        if not isinstance(self.status_file, str) or not self.status_file:
            raise ValueError("status_file must be a path")

    def command(self) -> list[str]:
        result = [self.sweep_binary, "-l", str(self.lna_gain), "-g", str(self.vga_gain),
                  "-w", str(self.bin_width_hz), "-P", "estimate"]
        if self.serial:
            result += ["-d", self.serial]
        for low, high in self.ranges_mhz:
            result += ["-f", f"{low}:{high}"]
        return result

    @property
    def excluded_hz(self) -> tuple[tuple[float, float], ...]:
        return tuple((lo * 1000000, hi * 1000000) for lo, hi in self.excluded_mhz)
