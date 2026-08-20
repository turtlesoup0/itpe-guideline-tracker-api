"""제외 분석 후보 도출 테스트.

핵심 계약: 살아있는 제목에 한 번이라도 걸리는 패턴은 근거가 아무리 많아도
후보가 되면 안 된다. ('지침', '개정' 같은 단어를 규칙으로 승격시키면
정상 문서가 조용히 사라진다 — LR-010)
"""

from app.models.guideline import ExclusionCategory
from app.services.exclusion_analysis import build_candidates, extract_ngrams

PHYS = ExclusionCategory.PHYSICAL_SECURITY


def _patterns(cands):
    return {c.pattern for c in cands}


def test_generic_word_in_active_titles_is_never_promoted():
    excluded = [
        ("정부청사 출입보안지침 일부개정 알림", PHYS),
        ("제2청사 출입보안 운영지침 개정", PHYS),
    ]
    active = ["개인정보 안전성 확보조치 기준 해설서", "클라우드 보안 지침 개정 안내"]

    patterns = _patterns(build_candidates(excluded, active))

    # 활성 제목에도 나오는 일반 단어는 제외
    assert "지침" not in patterns
    assert "개정" not in patterns
    # 제외 항목에만 2건 나오는 신호는 후보
    assert any("출입" in p for p in patterns)


def test_single_occurrence_is_not_a_rule():
    excluded = [("정부청사 출입보안지침 개정", PHYS)]
    assert build_candidates(excluded, ["무관한 제목"]) == []


def test_candidate_carries_evidence_and_category():
    excluded = [
        ("한국-베트남 상호인정협정 체결", ExclusionCategory.INTL_AGREEMENT),
        ("방송통신기자재 상호인정협정 개정", ExclusionCategory.INTL_AGREEMENT),
    ]
    cands = build_candidates(excluded, ["개인정보 보호 안내서"])
    top = cands[0]
    assert top.support_count == 2
    assert top.false_positive_count == 0
    assert top.category == ExclusionCategory.INTL_AGREEMENT
    assert top.sample_titles


def test_ngrams_cover_compound_nouns():
    grams = extract_ngrams("출입보안지침")
    assert "출입" in grams and "출입보안" in grams
