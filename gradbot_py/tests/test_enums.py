"""Tests for gradbot enum types: Lang, Country, Gender, AudioFormat."""

import gradbot


class TestLang:
    EXPECTED_CODES = {"En": "en", "Fr": "fr", "Es": "es", "De": "de", "Pt": "pt"}

    def test_all_variants_exist(self):
        for name in self.EXPECTED_CODES:
            assert hasattr(gradbot.Lang, name)

    def test_code_returns_correct_strings(self):
        for name, code in self.EXPECTED_CODES.items():
            assert getattr(gradbot.Lang, name).code() == code

    def test_rewrite_rules_matches_code(self):
        for name in self.EXPECTED_CODES:
            lang = getattr(gradbot.Lang, name)
            assert lang.rewrite_rules == lang.code()

    def test_equality(self):
        assert gradbot.Lang.En == gradbot.Lang.En
        assert gradbot.Lang.En != gradbot.Lang.Fr

    def test_hashable(self):
        s = {gradbot.Lang.En, gradbot.Lang.Fr, gradbot.Lang.En}
        assert len(s) == 2


class TestCountry:
    EXPECTED = {
        "Us": ("us", "United States"),
        "Gb": ("gb", "United Kingdom"),
        "Fr": ("fr", "France"),
        "De": ("de", "Germany"),
        "Mx": ("mx", "Mexico"),
        "Es": ("es", "Spain"),
        "Br": ("br", "Brazil"),
    }

    def test_all_variants_exist(self):
        for name in self.EXPECTED:
            assert hasattr(gradbot.Country, name)

    def test_code_returns_correct_strings(self):
        for name, (code, _) in self.EXPECTED.items():
            assert getattr(gradbot.Country, name).code() == code

    def test_str_returns_full_name(self):
        for name, (_, full_name) in self.EXPECTED.items():
            assert str(getattr(gradbot.Country, name)) == full_name


class TestGender:
    def test_variants_exist(self):
        assert hasattr(gradbot.Gender, "Masculine")
        assert hasattr(gradbot.Gender, "Feminine")

    def test_str(self):
        assert str(gradbot.Gender.Masculine) == "Masculine"
        assert str(gradbot.Gender.Feminine) == "Feminine"

    def test_equality(self):
        assert gradbot.Gender.Masculine == gradbot.Gender.Masculine
        assert gradbot.Gender.Masculine != gradbot.Gender.Feminine


class TestAudioFormat:
    def test_variants_exist(self):
        assert hasattr(gradbot.AudioFormat, "OggOpus")
        assert hasattr(gradbot.AudioFormat, "Pcm")

    def test_equality(self):
        assert gradbot.AudioFormat.OggOpus == gradbot.AudioFormat.OggOpus
        assert gradbot.AudioFormat.OggOpus != gradbot.AudioFormat.Pcm
