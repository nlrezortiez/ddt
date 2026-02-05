import sys
import signal
import subprocess
import threading
import queue

from gpiozero import Device, Button
import gpiozero.pins.lgpio
import lgpio

def __patched_init(self, chip=None):
    gpiozero.pins.lgpio.LGPIOFactory.__bases__[0].__init__(self)
    chip = 0
    self._handle = lgpio.gpiochip_open(chip)
    self._chip = chip
    self.pin_class = gpiozero.pins.lgpio.LGPIOPin

gpiozero.pins.lgpio.LGPIOFactory.__init__ = __patched_init

import ssd1306oled
import parser

LINE_QUEUE: queue.Queue[str] = queue.Queue(maxsize=10000)
should_stop: bool = False

b_plus = Button(5, pull_up=True, bounce_time=0.05)
b_minus = Button(6, pull_up=True, bounce_time=0.05)
b_switch = Button(13, pull_up=True, bounce_time=0.05)

def b_plus_press():
    parser.threshold += 1.0


def b_minus_press():
    parser.threshold -= 1.0


def b_switch_press():
    print("switch")


b_plus.when_pressed = b_plus_press
b_minus.when_pressed = b_minus_press
b_switch.when_pressed = b_switch_press

def stop_handler(signum, frame):
    raise KeyboardInterrupt

def drop_all_data() -> None:
    try:
        while True:
            LINE_QUEUE.get_nowait()
    except queue.Empty:
        pass


def reader_thread() -> None:
    header = sys.stdin.readline()
    for raw_line in sys.stdin:
        line = raw_line.strip("\n")
        try:
            LINE_QUEUE.put(line, block=False)
        except queue.Full:
            drop_all_data()


def main() -> None:
    t = threading.Thread(target=reader_thread, daemon=True)
    t.start()

    no_fpv_layout: bool = False
    display = ssd1306oled.init_display()

    while True:
        try:
            line = LINE_QUEUE.get(timeout=0.1)
        except queue.Empty:
            continue

        res = parser.parse(line)
        if res is not None:
            ssd1306oled.FPV_Detected(display, res, parser.threshold)
            no_fpv_layout = False
            drop_all_data()
        elif res is None and not no_fpv_layout:
            ssd1306oled.FPV_Layout(display, parser.threshold)
            no_fpv_layout = True


if __name__ == "__main__":
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    main()
