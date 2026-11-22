"""The local AI server (tools/local_ai_server.py) and the ai_server.mode switch: no models needed, the backends are fakes.

The robot's own clients (LLMClient, Speech) talk to the server over a real socket, so these tests show that the protocol is the
AI server's: a client that works with that server works with this one."""

import base64
import http.server
import io
import json
import threading
import unittest
import wave

import numpy as np

from robot.config import load_config
from robot.services.llm import LLMClient, LLMError, to_prompt
from robot.services.speech import Speech
from tools import local_ai_server as lai
from vendor.ai_server import stt, tts
from vendor.ai_server.llm import request


def wav_bytes(seconds=0.5, rate=22050, channels=1, width=2) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x01\x02\x03\x04"[:width] * channels * int(rate * seconds))
    return out.getvalue()


class FakeLLM:
    def __init__(self, reply="Hello there!"):
        self.reply, self.prompts = reply, []

    def generate(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeSTT:
    def __init__(self):
        self.calls = []

    def transcribe(self, pcm, dtype, rate, channels):
        self.calls.append((len(pcm), dtype, rate, channels))
        return "where is dune"


class FakeTTS:
    name = "en_US-ryan-medium"

    def __init__(self):
        self.texts = []

    def synthesize(self, text):
        self.texts.append(text)
        return wav_bytes(), 22050


class ParsePromptTests(unittest.TestCase):
    def test_round_trips_what_the_robot_sends(self):
        messages = [{"role": "system", "content": "You are Stuart.\n\nBe brief."},
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "Hello!"},
                    {"role": "user", "content": "Catalogue entry:\nTitle: Dune\n\nThe visitor asked what this is about."}]
        self.assertEqual(lai.parse_prompt(to_prompt(messages)), messages)

    def test_text_without_roles_is_one_user_message(self):
        self.assertEqual(lai.parse_prompt("Say hello."), [{"role": "user", "content": "Say hello."}])

    def test_the_trailing_assistant_cue_is_dropped(self):
        self.assertEqual(lai.parse_prompt("User: hi\n\nAssistant:"), [{"role": "user", "content": "hi"}])


class PcmTests(unittest.TestCase):
    def convert(self, samples, dtype, rate, channels):
        out = lai.to_whisper_wav(samples.tobytes(), dtype, rate, channels)
        with wave.open(io.BytesIO(out)) as w:
            return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()

    def test_48k_stereo_becomes_16k_mono_16bit(self):
        rate, channels, width, frames = self.convert(np.zeros(48000 * 2, "<i2"), "int16", 48000, 2)
        self.assertEqual((rate, channels, width), (16000, 1, 2))
        self.assertAlmostEqual(frames, 16000, delta=4)

    def test_16k_mono_keeps_its_length(self):
        self.assertEqual(self.convert(np.zeros(8000, "<i2"), "int16", 16000, 1)[3], 8000)

    def test_other_dtypes(self):
        self.assertAlmostEqual(self.convert(np.full(4000, 128, "u1"), "uint8", 8000, 1)[3], 8000, delta=4)
        self.assertAlmostEqual(self.convert(np.zeros(16000, "<i4"), "int32", 16000, 1)[3], 16000, delta=1)

    def test_a_tone_survives_the_conversion(self):
        t = np.arange(48000) / 48000.0
        tone = (np.sin(2 * np.pi * 440 * t) * 20000).astype("<i2")
        out = lai.to_whisper_wav(tone.tobytes(), "int16", 48000, 1)
        with wave.open(io.BytesIO(out)) as w:
            peak = np.abs(np.frombuffer(w.readframes(w.getnframes()), "<i2")).max()
        self.assertGreater(peak, 15000)

    def test_unknown_dtype_is_refused(self):
        with self.assertRaises(ValueError):
            lai.to_whisper_wav(b"\0\0", "float32", 16000, 1)

    def test_whisper_noise_tags_are_not_words(self):
        self.assertEqual(lai.clean_transcript(" [BLANK_AUDIO] "), "")
        self.assertEqual(lai.clean_transcript("(music) where is  Dune? [ Silence ]"), "where is Dune?")


class FakeLlamaServer(http.server.BaseHTTPRequestHandler):
    seen = []
    reply = "<think>\n\n</think>\n\nIt is a desert story."

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLlamaServer.seen.append(body)
        data = json.dumps({"choices": [{"message": {"content": FakeLlamaServer.reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        data = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class LlamaBackendTests(unittest.TestCase):
    def setUp(self):
        FakeLlamaServer.seen, FakeLlamaServer.reply = [], "<think>\n\n</think>\n\nIt is a desert story."
        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaServer)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.backend = lai.LlamaBackend(f"http://127.0.0.1:{self.http.server_address[1]}", max_tokens=99)

    def test_sends_chat_messages_with_thinking_off_and_strips_think_tags(self):
        reply = self.backend.generate("System: Be brief.\n\nUser: What is Dune?\n\nAssistant:")
        self.assertEqual(reply, "It is a desert story.")
        body = FakeLlamaServer.seen[0]
        self.assertEqual(body["messages"], [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "What is Dune?"}])
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["max_tokens"], 99)

    def test_ready(self):
        self.assertTrue(self.backend.ready())
        self.assertFalse(lai.LlamaBackend("http://127.0.0.1:9").ready())

    def test_empty_reply_and_unreachable_server_are_backend_errors(self):
        FakeLlamaServer.reply = "<think>x</think>"
        with self.assertRaises(lai.BackendError):
            self.backend.generate("hi")
        with self.assertRaises(lai.BackendError):
            lai.LlamaBackend("http://127.0.0.1:9", timeout=1).generate("hi")


class ProtocolTests(unittest.TestCase):
    """The robot's own clients against the server over a real socket."""

    def setUp(self):
        self.llm, self.stt, self.tts = FakeLLM(), FakeSTT(), FakeTTS()
        self.server = lai.make_server(lai.Handler(self.llm, self.stt, self.tts), "127.0.0.1", 0, timeout=5)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_llm_through_the_robots_client(self):
        messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
        self.assertEqual(LLMClient("127.0.0.1", self.port, 5).chat(messages), "Hello there!")
        self.assertEqual(self.llm.prompts, [to_prompt(messages)])

    def test_llm_failure_is_the_protocols_error_response(self):
        self.llm.reply = lai.BackendError("the llama-server is down")
        with self.assertRaises(LLMError) as caught:
            LLMClient("127.0.0.1", self.port, 5).chat([{"role": "user", "content": "hi"}])
        self.assertIn("the llama-server is down", str(caught.exception))

    def test_tts_returns_a_wav_the_robot_accepts(self):
        wav = Speech("127.0.0.1", self.port, "af_heart", 5, engine="kokoro").synthesize("Hello there")
        with wave.open(io.BytesIO(wav)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (22050, 1, 2))
        self.assertEqual(self.tts.texts, ["Hello there"])

    def test_tts_response_passes_the_vendor_validation(self):
        response = request("127.0.0.1", self.port, "TTS", {"voice": "af_heart"}, "Hi", timeout=5)
        self.assertEqual(response["parameters"]["format"], "wav")
        tts.validate_wav(response["content"], response["parameters"])

    def test_stt_through_the_robots_client(self):
        text = Speech("127.0.0.1", self.port, timeout=5).transcribe(wav_bytes(1.0, rate=16000))
        self.assertEqual(text, "where is dune")
        self.assertEqual(self.stt.calls, [(32000, "int16", 16000, 1)])

    def test_stt_reads_what_vendor_stt_sends(self):
        pcm, dtype, rate, channels, _ = stt.read_wav(self._write(wav_bytes(0.2, rate=44100, channels=2)))
        params = dict(format="raw_pcm", dtype=dtype, sample_rate=rate, channels=channels)
        self.assertEqual(request("127.0.0.1", self.port, "STT", params, base64.b64encode(pcm).decode(), timeout=5)["content"], "where is dune")

    def _write(self, data):
        import tempfile
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        self.addCleanup(__import__("os").unlink, f.name)
        f.write(data)
        f.close()
        return f.name

    def test_face_and_emotion_are_refused_with_the_error_response(self):
        for command in ("FACE", "EMOTION"):
            with self.assertRaises(ValueError) as caught:
                request("127.0.0.1", self.port, command, {"operation": "detect", "format": "jpeg"}, "AAAA", timeout=5)
            self.assertIn("not handled by the local AI server", str(caught.exception))

    def test_bad_requests_are_error_responses_not_dropped_connections(self):
        bad = [("TTS", {}, "   "), ("TTS", {}, "x" * 2001), ("STT", {"format": "wav"}, "AAAA"),
               ("STT", {"format": "raw_pcm", "dtype": "int16", "sample_rate": 16000, "channels": 1}, "not base64!"),
               ("NOPE", {}, "x"), ("LLM", {}, "")]
        for command, parameters, content in bad:
            with self.assertRaises(ValueError, msg=(command, content[:10])):
                request("127.0.0.1", self.port, command, parameters, content, timeout=5)

    def test_one_connection_can_carry_several_requests(self):
        import socket
        from vendor.ai_server.llm import receive_json, send_json
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            for n in range(3):
                send_json(sock, {"robot_id": "t", "request_id": str(n), "command": ["LLM"], "parameters": {}, "content": "hi"})
                response = receive_json(sock)
                self.assertEqual((response["request_id"], response["command"], response["content"]), (str(n), [], "Hello there!"))


class ModeSwitchTests(unittest.TestCase):
    def test_remote_is_the_default_and_changes_nothing(self):
        cfg = load_config(env={})
        self.assertEqual(cfg.ai_mode, "remote")
        self.assertEqual(cfg.robot["perception"]["detector"], "server")
        self.assertNotEqual(cfg.ai_host, "127.0.0.1")

    def test_local_uses_the_local_address_and_detects_faces_on_the_robot(self):
        cfg = load_config(env={"AI_MODE": "local"})
        self.assertEqual((cfg.ai_mode, cfg.ai_host, cfg.ai_port), ("local", "127.0.0.1", 7898))
        self.assertEqual(cfg.robot["perception"]["detector"], "local")

    def test_ai_host_and_port_still_win(self):
        cfg = load_config(env={"AI_MODE": "local", "AI_HOST": "10.1.2.3", "AI_PORT": "9000"})
        self.assertEqual((cfg.ai_host, cfg.ai_port), ("10.1.2.3", 9000))

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            load_config(env={"AI_MODE": "cloud"})


if __name__ == "__main__":
    unittest.main()
