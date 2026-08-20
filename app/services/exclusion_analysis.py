"""제외 항목 주기 분석 — "왜 불필요한 항목이 새어 들어오는가".

입력은 사람이 수동 제외한 항목이다. 자동 제외(it_domain, regex_exclude, llm)는
필터가 제대로 동작한 결과라 배울 것이 없다. 배울 것은 "필터를 통과했는데
사람이 아니라고 판단한" 항목뿐이다.

절차:
  1) 제외된 제목에서 후보 문자열(한글 2~4자 부분문자열 + 영문/숫자 토큰) 추출
  2) support   = 그 문자열을 포함하는 '제외된' 제목 수
  3) false_pos = 그 문자열을 포함하는 '살아있는' 제목 수
  4) support >= MIN_SUPPORT 이고 false_pos == 0 인 것만 후보로 남김

false_pos 를 세는 것이 이 분석의 핵심이다. 제외 제목만 보고 빈발 문자열을
뽑으면 '개정', '지침' 같은 단어가 1등으로 올라오고, 그걸 규칙으로 만들면
정상 문서 수백 건이 조용히 사라진다. 살아있는 제목에 한 번이라도 걸리는
문자열은 근거가 아무리 많아도 후보에서 뺀다.

MIN_SUPPORT 를 1로 낮추지 말 것. 사례 하나로 만든 규칙은 근거가 아니라
그 한 건을 다시 쓴 것에 불과하다.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exclusion_rule import ExclusionRuleCandidate, RuleCandidateStatus
from app.models.guideline import ExclusionCategory, Guideline

logger = logging.getLogger(__name__)

MIN_SUPPORT = 2          # 최소 근거 건수 — 1은 규칙이 아니라 그 항목 자체다
MAX_CANDIDATES = 40      # 사람이 한 번에 검토할 수 있는 분량
MIN_GRAM = 2
MAX_GRAM = 4
MAX_SAMPLES = 3

_WORD_RE = re.compile(r"[가-힣]+|[A-Za-z][A-Za-z0-9]{1,}")


@dataclass
class Candidate:
    pattern: str
    support_count: int
    false_positive_count: int
    category: ExclusionCategory | None
    sample_titles: list[str] = field(default_factory=list)


def extract_ngrams(title: str) -> set[str]:
    """제목에서 규칙 후보가 될 만한 문자열을 뽑는다.

    한글은 붙여쓰기 합성어("출입보안지침")가 많아 단어 단위로는 일반화가
    안 된다. 그래서 2~4자 부분문자열까지 낸 뒤, support/false_positive
    필터가 지나치게 구체적인 것과 지나치게 일반적인 것을 각각 걸러내게 한다.
    """
    grams: set[str] = set()
    for word in _WORD_RE.findall(title):
        if word.isascii():
            if len(word) >= MIN_GRAM:
                grams.add(word.lower())
            continue
        for size in range(MIN_GRAM, MAX_GRAM + 1):
            for i in range(len(word) - size + 1):
                grams.add(word[i : i + size])
    return grams


def build_candidates(
    excluded: list[tuple[str, ExclusionCategory | None]],
    active_titles: list[str],
) -> list[Candidate]:
    """제외/활성 제목으로부터 규칙 후보를 만든다 (순수 함수 — 테스트 대상)."""
    support: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    categories: dict[str, Counter[ExclusionCategory]] = {}

    for title, category in excluded:
        for gram in extract_ngrams(title):
            support[gram] += 1
            samples.setdefault(gram, [])
            if len(samples[gram]) < MAX_SAMPLES:
                samples[gram].append(title)
            if category is not None:
                categories.setdefault(gram, Counter())[category] += 1

    frequent = {g for g, n in support.items() if n >= MIN_SUPPORT}
    if not frequent:
        return []

    false_pos: Counter[str] = Counter()
    lowered_active = [(t, t.lower()) for t in active_titles]
    for gram in frequent:
        needle = gram if not gram.isascii() else gram.lower()
        for original, lowered in lowered_active:
            haystack = lowered if gram.isascii() else original
            if needle in haystack:
                false_pos[gram] += 1

    candidates = [
        Candidate(
            pattern=gram,
            support_count=support[gram],
            false_positive_count=false_pos[gram],
            category=(
                categories[gram].most_common(1)[0][0] if categories.get(gram) else None
            ),
            sample_titles=samples.get(gram, []),
        )
        for gram in frequent
        if false_pos[gram] == 0
    ]

    # 근거가 많고, 더 구체적인(긴) 패턴을 먼저 보여준다
    candidates.sort(key=lambda c: (-c.support_count, -len(c.pattern), c.pattern))
    return _drop_redundant(candidates)[:MAX_CANDIDATES]


def _drop_redundant(candidates: list[Candidate]) -> list[Candidate]:
    """같은 근거를 설명하는 부분문자열 중복을 줄인다.

    '출입보안', '출입보', '입보안'이 모두 support 2로 올라오면 사람이 볼 게
    셋으로 늘 뿐이다. 더 긴 패턴이 이미 채택됐고 근거 수가 같으면 짧은 쪽은
    버린다.
    """
    kept: list[Candidate] = []
    for cand in candidates:
        covered = any(
            cand.pattern in k.pattern and cand.support_count == k.support_count
            for k in kept
        )
        if not covered:
            kept.append(cand)
    return kept


async def analyze_exclusions(db: AsyncSession) -> list[Candidate]:
    """DB에서 제외/활성 제목을 읽어 후보를 만든다."""
    excluded_rows = (
        await db.execute(
            select(Guideline.title, Guideline.exclusion_category)
            .where(Guideline.excluded_at.is_not(None))
        )
    ).all()
    active_titles = list(
        (
            await db.execute(
                select(Guideline.title).where(Guideline.excluded_at.is_(None))
            )
        ).scalars()
    )

    excluded = [(row[0], row[1]) for row in excluded_rows]
    logger.info(
        "제외 분석: 제외 %d건 / 활성 %d건", len(excluded), len(active_titles)
    )
    if len(excluded) < MIN_SUPPORT:
        logger.info("제외 항목이 %d건뿐 — 후보를 만들지 않는다", len(excluded))
        return []

    return build_candidates(excluded, active_titles)


async def store_candidates(
    db: AsyncSession, candidates: list[Candidate]
) -> dict[str, int]:
    """후보를 upsert 한다. 사람이 이미 판단(승인/반려)한 패턴은 건드리지 않는다."""
    existing_rows = (
        await db.execute(select(ExclusionRuleCandidate))
    ).scalars().all()
    existing = {row.pattern: row for row in existing_rows}

    created = updated = skipped = 0
    for cand in candidates:
        row = existing.get(cand.pattern)
        if row is None:
            db.add(
                ExclusionRuleCandidate(
                    pattern=cand.pattern,
                    category=cand.category,
                    support_count=cand.support_count,
                    false_positive_count=cand.false_positive_count,
                    sample_titles="\n".join(cand.sample_titles),
                    status=RuleCandidateStatus.PENDING,
                )
            )
            created += 1
            continue

        if row.status != RuleCandidateStatus.PENDING:
            skipped += 1
            continue

        row.category = cand.category
        row.support_count = cand.support_count
        row.false_positive_count = cand.false_positive_count
        row.sample_titles = "\n".join(cand.sample_titles)
        updated += 1

    return {"created": created, "updated": updated, "skipped": skipped}


async def load_approved_patterns(db: AsyncSession) -> list[str]:
    """사람이 승인한 제외 패턴 목록.

    크롤 1회당 한 번만 읽어 제목 매칭에 쓴다. 승인되지 않은 후보는 절대
    포함하지 않는다 — 후보는 제안일 뿐이다.
    """
    rows = (
        await db.execute(
            select(ExclusionRuleCandidate.pattern).where(
                ExclusionRuleCandidate.status == RuleCandidateStatus.APPROVED
            )
        )
    ).scalars().all()
    return list(rows)


def load_approved_patterns_sync(db) -> list[str]:
    """load_approved_patterns 의 동기 버전 (Celery 워커 경로용)."""
    return [
        row[0]
        for row in db.query(ExclusionRuleCandidate.pattern)
        .filter(ExclusionRuleCandidate.status == RuleCandidateStatus.APPROVED)
        .all()
    ]


def matched_approved_pattern(title: str, patterns: list[str]) -> str | None:
    """제목에 걸리는 승인 패턴을 돌려준다 (없으면 None)."""
    lowered = title.lower()
    for pattern in patterns:
        needle = pattern.lower() if pattern.isascii() else pattern
        haystack = lowered if pattern.isascii() else title
        if needle in haystack:
            return pattern
    return None
