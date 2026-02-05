from time import sleep

from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas


def init_display():
    serial = i2c(port=1, address=0x3C)
    device = ssd1306(serial, width=128, height=64)
    device.clear()
    return device

def clear_display(device) -> None:
    with canvas(device) as draw:
        draw.rectangle([(0, 16), (50, 50)], fill="black")

def switch_FPV_Active(device, draw: canvas, threshold: float) -> None:
    draw.rounded_rectangle([(0, 0), (28, 15)], fill="white", radius=1)
    draw.text((3, 1), "FPV", fill="black", font_size=12)
    draw.rounded_rectangle([(32, 0), (60, 15)], fill="black", radius=1, outline="white")
    draw.text((33, 1), "SWP", fill="white", font_size=12)
    draw.text((63, 1), f"THRS={threshold:.2f}MHz", fill="white", font_size=12)


def switch_SWP_Active(device, draw: canvas) -> None:
    draw.rounded_rectangle([(32, 0), (60, 15)], fill="white", radius=1)
    draw.text((33, 1), "SWP", fill="black", font_size=12)
    draw.rounded_rectangle([(0, 0), (28, 15)], fill="black", radius=1, outline="white")
    draw.text((3, 1), "FPV", fill="white", font_size=12)


def FPV_Detected(device, data: tuple[float, float] | None, threshold: float) -> None:
    with canvas(device) as draw:
        switch_FPV_Active(device, draw, threshold)
        draw.text((40, 20), "FPV", fill="white", font_size=24)
        draw.text((20, 46), f"F = {data[0]} MHz", fill="white", font_size=15)

    sleep(3)


def FPV_Layout(device, threshold: float):
    with canvas(device) as draw:
        switch_FPV_Active(device, draw, threshold)

def SWP_Layout(device):
    with canvas(device) as draw:
        switch_SWP_Active(device, draw)

def main(device):
    while True:
        FPV_Layout(device)


if __name__ == "__main__":
    device = init_display()
    while True:
        FPV_Layout(device)
