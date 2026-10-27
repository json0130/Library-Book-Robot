"""Outputs: voice, face/screen, lights, neck, storage ring, printer, navigation.

Actuators.apply_style() sets the ambient behaviour for a StyleVector (lights, neck, layout,
speed); Actuators.deliver() sends one message through the style's channels.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Actuators:
    voice: object
    face: object
    lights: object
    neck: object
    ring: object
    printer: object
    nav: object

    def apply_style(self, style) -> None:
        self.lights.set(style.light_color, style.brightness)
        self.neck.pose(style.neck_pose)
        self.face.set_layout(style.screen_layout)
        self.nav.set_speed_limit(style.speed_limit)

    def deliver(self, text: str, style) -> list[str]:
        """Send a message through each of the style's channels; returns the channels used."""
        used = []
        for channel in style.channels:
            if channel == "speech":
                self.voice.say(text, style)
            elif channel == "screen":
                self.face.show_text(text)
            elif channel == "print":
                self.printer.print_slip(text)
            elif channel == "spotlight":
                self.lights.spotlight(True)
            elif channel == "gaze":
                self.neck.look_at("visitor")
            else:
                continue
            used.append(channel)
        return used
