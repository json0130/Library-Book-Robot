import unittest

from robot.core.expression import EMOTIONS, blend, normalize


class BlendTest(unittest.TestCase):
    def test_no_signals_is_neutral(self):
        self.assertEqual(blend(), "neutral")

    def test_reply_tag_alone(self):
        for e in EMOTIONS:
            self.assertEqual(blend(reply_tag=e), e)
        self.assertEqual(blend(reply_tag="Surprised"), "surprise")      # aliases
        self.assertEqual(blend(reply_tag="confused"), "neutral")        # unknown tag ignored

    def test_confident_user_emotion_alone_is_mirrored_or_answered(self):
        self.assertEqual(blend("happy", 0.9), "happy")
        self.assertEqual(blend("sad", 0.9), "sad")
        self.assertEqual(blend("surprise", 0.9), "surprise")
        self.assertEqual(blend("angry", 0.9), "sad")       # concern, not anger back
        self.assertEqual(blend("fear", 0.9), "sad")
        self.assertEqual(blend("disgust", 0.9), "neutral")

    def test_weak_user_emotion_stays_neutral(self):
        self.assertEqual(blend("happy", 0.4), "neutral")   # 0.5*0.4 = 0.2 < 0.25
        self.assertEqual(blend("happy", 0.5), "happy")     # tie at 0.25 goes to the user emotion
        self.assertEqual(blend("happy", 7), "happy")       # confidence clamped to 1

    def test_tag_beats_a_different_user_emotion(self):
        self.assertEqual(blend("sad", 1.0, "happy"), "happy")    # 0.6 > 0.5
        self.assertEqual(blend("happy", 1.0, "surprise"), "surprise")

    def test_agreeing_signals_add_up(self):
        # user happy 0.9 (0.45) + tag happy (0.6) easily beats neutral
        self.assertEqual(blend("happy", 0.9, "happy"), "happy")
        # a neutral tag with a confident happy visitor: neutral 0.25 + 0.6 > happy 0.45
        self.assertEqual(blend("happy", 0.9, "neutral"), "neutral")

    def test_normalize(self):
        self.assertEqual(normalize(" SCARED "), "fear")
        self.assertIsNone(normalize(None))
        self.assertIsNone(normalize("confused"))


if __name__ == "__main__":
    unittest.main()
