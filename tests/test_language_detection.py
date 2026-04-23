import pytest
from core.language_detection import detect_language, SUPPORTED_LANGUAGES


def test_detects_french():
    assert detect_language("Bonjour, merci pour votre message, je suis intéressé", "fr") == "fr"


def test_detects_german():
    assert detect_language("Guten Tag, vielen Dank für Ihre Nachricht. Ich bin interessiert.", "de") == "de"


def test_detects_lithuanian():
    assert detect_language("Labas, ačiū už laišką, esu suinteresuotas.", "lt") == "lt"


def test_detects_estonian():
    assert detect_language("Tere, tänan teie sõnumi eest. Olen huvitatud.", "et") == "et"


def test_detects_latvian():
    assert detect_language("Sveiki, paldies par jūsu vēstuli. Esmu ieinteresēts.", "lv") == "lv"


def test_detects_english():
    assert detect_language("Hello, thanks for your message. I'm interested.", "en") == "en"


def test_hint_used_when_detection_matches():
    # When detection agrees with hint, hint wins (cheaper branch)
    assert detect_language("Bonjour à tous, merci beaucoup", "fr") == "fr"


def test_hint_used_when_text_too_short():
    # Very short text is unreliable - prefer hint
    assert detect_language("Ok", "fr") == "fr"


def test_detected_language_overrides_hint_when_mismatch():
    # Lead wrote English even though campaign was FR - respect user's actual language
    fr_hint = "fr"
    english_text = "Hello, I'm interested in your services. Could you share more information about pricing?"
    assert detect_language(english_text, fr_hint) == "en"


def test_unsupported_language_falls_back_to_en():
    # Arabic is not in supported set; should fallback
    arabic_text = "مرحبا كيف حالك شكرا جزيلا"
    result = detect_language(arabic_text, "fr")
    assert result in ("en", "fr")  # either fallback-en or hint-fr acceptable


def test_empty_text_returns_hint():
    assert detect_language("", "fr") == "fr"
    assert detect_language("   ", "lt") == "lt"


def test_supported_languages_contains_expected():
    expected = {"lt", "en", "fr", "de", "et", "lv"}
    assert expected.issubset(SUPPORTED_LANGUAGES)
