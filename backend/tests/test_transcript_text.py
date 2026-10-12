from app.services.transcript_text import normalize_transcript_text


def test_transcript_text_formats_sentences_commas_and_final_punctuation() -> None:
    assert normalize_transcript_text("  xin chào ,mọi người. hôm nay họp nhé  ") == (
        "Xin chào, mọi người. Hôm nay họp nhé."
    )


def test_transcript_text_removes_conservative_stutters_without_changing_emphasis() -> None:
    assert normalize_transcript_text("tôi tôi muốn muốn muốn nghỉ, rất rất mệt") == (
        "Tôi muốn nghỉ, rất rất mệt."
    )


def test_transcript_text_does_not_break_decimal_or_web_address() -> None:
    assert normalize_transcript_text("phiên bản 1.5 ở example.com") == (
        "Phiên bản 1.5 ở example.com."
    )
