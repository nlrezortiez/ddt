from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas


import signal


def release_display(signum, frame):
    device.clear()


serial = i2c(port=1, address=0x3C)
device = ssd1306(serial, width=128, height=64)
device.clear()

signal.signal(signal.SIGINT, release_display)
signal.signal(signal.SIGTERM, release_display)


while True:
    with canvas(device) as draw:
        draw.text((25, 25), "Reset HackRF", fill="white", font_size=18)
