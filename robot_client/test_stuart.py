"""
test_stuart.py — robots/stuart.py with a fake TTS, speaker and face.

    python3 -m pytest test_stuart.py
"""

import threading

from robots.stuart import Speech, face_for


class _TTS:
    pace = 1.12

    def __init__(self, fail=False):
        self.made, self.fail = [], fail

    def synthesize(self, text):
        self.made.append(text)
        if self.fail:
            raise OSError("connection refused")
        return f"wav:{text}".encode()


class _Audio:
    def __init__(self):
        self.played = []

    def play(self, wav, volume):
        self.played.append(wav.decode())


class _Face:
    def __init__(self):
        self.calls = []

    def talk_audio(self, wav): self.calls.append("audio")
    def talk_text(self, text): self.calls.append("text")
    def stop_talking(self): self.calls.append("stop")


class _Client:
    def __init__(self):
        self.is_speaking, self.tts_started_event = threading.Event(), threading.Event()

    def on_tts_state(self, speaking): pass


def _speak(tts, text, **cfg):
    audio, face = _Audio(), _Face()
    speech = Speech("speech", {"words_per_second": 100, **cfg}, tts, audio, face)
    speech.set_client(_Client())
    speech.start()
    try:
        done = threading.Event()
        speech.speak_with_callback(text, callback=done.set)
        assert done.wait(3)                            # the demo step's ACK still fires
    finally:
        speech.stop()
    return audio, face, speech


def test_server_tags_become_faces_stuart_has():
    assert [face_for(t) for t in ("HAPPY", "[SURPRISED]", "wave", "CONFUSED", "DEFAULT")] == \
        ["happy", "surprise", "happy", "thinking", "neutral"]
    assert face_for("DANCE") is None


def test_each_sentence_is_played_in_order_and_the_mouth_follows_the_sound():
    tts = _TTS()
    audio, face, _ = _speak(tts, "[HAPPY] Hello there. The atlas is on level two.")
    assert audio.played == ["wav:Hello there.", "wav:The atlas is on level two."]
    assert tts.made == ["Hello there.", "The atlas is on level two."]   # each made once: the second while the first played
    assert face.calls == ["audio", "audio"]


def test_without_tts_the_face_still_talks_and_nothing_is_played():
    audio, face, _ = _speak(_TTS(fail=True), "One. Two.")
    assert audio.played == []
    assert face.calls == ["text", "text"]


def test_a_persona_rate_changes_the_pace():
    tts = _TTS()
    speech = Speech("speech", {}, tts, _Audio(), _Face())
    speech.update_voice_config({"rate": "+25%"})
    assert abs(tts.pace - 1.4) < 1e-9
    speech.update_voice_config({"rate": "-10%"})
    assert abs(tts.pace - 1.008) < 1e-9
