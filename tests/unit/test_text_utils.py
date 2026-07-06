from __future__ import annotations

from src.worktrace.utils.text import clean_text, normalize_sentence_final_ma


def test_normalize_sentence_final_ma_replaces_sentence_tail() -> None:
    assert normalize_sentence_final_ma("可以开始妈") == "可以开始吗"
    assert normalize_sentence_final_ma("可以开始妈？") == "可以开始吗？"
    assert normalize_sentence_final_ma('可以开始妈”') == '可以开始吗”'


def test_normalize_sentence_final_ma_does_not_replace_mid_sentence() -> None:
    assert normalize_sentence_final_ma("我妈今天来公司") == "我妈今天来公司"
    assert normalize_sentence_final_ma("妈妈说可以开始") == "妈妈说可以开始"


def test_clean_text_applies_sentence_final_ma_line_by_line() -> None:
    assert clean_text("第一句妈\n第二句妈？\n我妈今天来") == "第一句吗\n第二句吗？\n我妈今天来"
