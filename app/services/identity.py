"""
의미 기반 정체성 판정 엔진 — "이 항목은 기존 어떤 문서의 다른 판인가?"

3계층 구조 (위일수록 싸고 확실, 아래일수록 보편):
  Tier 0: URL/게시물ID·PDF 해시 동일  → guideline_sync의 기존 dedup (불변 식별자)
  fast path: normalize_title exact    → guideline_sync의 기존 버전 매칭
  Tier 1: 제목 임베딩 코사인 유사도   → 후보 생성 (recall 담당, 자동 병합 금지)
  Tier 2: 로컬 LLM 판정               → 최종 결정 (precision 담당)

캘리브레이션(2026-08-18, embedding.py 참조)상 SAME/DIFF 유사도 구간이
겹치므로 임계값만으로 병합을 결정하지 않는다 — 후보는 전부 LLM을 거친다.
"""

import logging

import httpx
from sqlalchemy import select

from app.services.embedding import (
    CANDIDATE_SIM_THRESHOLD,
    EMBED_DIM,
    EMBED_MODEL_NAME,
    cosine,
    encode_title,
    pack_vector,
    unpack_vector,
)
from app.services.llm_classifier import LLM_BASE_URL, LLM_TIMEOUT, _chat_payload

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 3  # LLM 판정에 올릴 상위 후보 수

SAME_DOC_PROMPT = """\
두 게시물 제목이 같은 문서의 다른 판(개정판·연도판·버전)인지 판단하세요.

기관: {agency_name}
제목 A (신규 수집): {title_a}
제목 B (기존 등록): {title_b}

판단 기준:
- 같은 문서의 개정판/연도판/판번호 차이면 YES (예: (2024.4.) vs (2026.4. 개정), 제8판 vs 제9판, 2024 백서 vs 2025 백서)
- [현재 안내서]/[과거 안내서] 태그, 새글 표시(N), 배포·개정·안내 문구 차이는 무시
- 주제가 비슷해도 서로 다른 문서면 NO
  (예: '가명정보 처리 가이드라인' vs '가명정보 결합·반출 안내서',
   '고영향 AI 영향평가 가이드라인' vs '고영향 AI 사업자 책무 가이드라인',
   '처리방침 작성지침' vs '처리방침 표준(안)')
- 규정명·대상이 다른 고시·규정이면 NO (예: 'A규정 제정 고시' vs 'B방법 폐지 고시')
- 본편과 해설서/사례집/별책/부록/요약본/영문판 관계면 NO
- 같은 사안의 서로 다른 보도 단계(국무회의 통과 vs 시행 등)면 NO

YES 또는 NO만 답하세요."""


# ── Tier 2: LLM 판정 ─────────────────────────────────────


def _parse_yes(data: dict) -> bool:
    choices = data.get("choices") or [{}]
    answer = (choices[0].get("message", {}).get("content") or "").strip().upper()
    return answer.startswith("YES")


def llm_same_document_sync(agency_name: str, title_a: str, title_b: str) -> bool | None:
    """같은 문서의 다른 판인지 LLM 판정 (동기). None = LLM 오류(판단 불가)."""
    prompt = SAME_DOC_PROMPT.format(agency_name=agency_name, title_a=title_a, title_b=title_b)
    try:
        with httpx.Client(timeout=httpx.Timeout(LLM_TIMEOUT)) as client:
            resp = client.post(f"{LLM_BASE_URL}/chat/completions", json=_chat_payload(prompt))
            resp.raise_for_status()
            return _parse_yes(resp.json())
    except Exception as e:
        logger.warning("동일문서 LLM 판정 실패: %s", e)
        return None


async def llm_same_document(agency_name: str, title_a: str, title_b: str) -> bool | None:
    """같은 문서의 다른 판인지 LLM 판정 (비동기). None = LLM 오류."""
    prompt = SAME_DOC_PROMPT.format(agency_name=agency_name, title_a=title_a, title_b=title_b)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(LLM_TIMEOUT)) as client:
            resp = await client.post(f"{LLM_BASE_URL}/chat/completions", json=_chat_payload(prompt))
            resp.raise_for_status()
            return _parse_yes(resp.json())
    except Exception as e:
        logger.warning("동일문서 LLM 판정 실패: %s", e)
        return None


# ── Tier 1: 임베딩 후보 생성 ─────────────────────────────


class EmbeddingIndex:
    """기관 단위 인메모리 임베딩 인덱스 (sync 배치 1회 로드 + 신규 항목 추가)."""

    def __init__(self) -> None:
        self._rows: list[tuple[int, str, list[float]]] = []  # (guideline_id, title, vector)

    def add(self, guideline_id: int, title: str, vector: list[float]) -> None:
        self._rows.append((guideline_id, title, vector))

    def candidates(self, title: str) -> list[tuple[int, str, float]]:
        """유사도 상위 후보 반환: [(guideline_id, title, sim)] (임계값 이상만)."""
        if not self._rows:
            return []
        q = encode_title(title)
        scored = [
            (gid, t, cosine(q, v))
            for gid, t, v in self._rows
        ]
        scored = [s for s in scored if s[2] >= CANDIDATE_SIM_THRESHOLD]
        scored.sort(key=lambda s: -s[2])
        return scored[:MAX_CANDIDATES]


def load_embedding_index_sync(db, agency_id: int, item_type) -> EmbeddingIndex:
    """동기 세션으로 기관의 임베딩 인덱스 로드 (item_type 동일 스코프)."""
    from app.models.guideline import Guideline
    from app.models.guideline_embedding import GuidelineEmbedding

    idx = EmbeddingIndex()
    rows = (
        db.query(GuidelineEmbedding, Guideline.item_type)
        .join(Guideline, Guideline.id == GuidelineEmbedding.guideline_id)
        .filter(Guideline.agency_id == agency_id)
        .all()
    )
    for emb, itype in rows:
        if itype == item_type and emb.model == EMBED_MODEL_NAME:
            idx.add(emb.guideline_id, emb.title, unpack_vector(emb.vector))
    return idx


async def load_embedding_index(db, agency_id: int, item_type) -> EmbeddingIndex:
    """비동기 세션으로 기관의 임베딩 인덱스 로드."""
    from app.models.guideline import Guideline
    from app.models.guideline_embedding import GuidelineEmbedding

    idx = EmbeddingIndex()
    result = await db.execute(
        select(GuidelineEmbedding, Guideline.item_type)
        .join(Guideline, Guideline.id == GuidelineEmbedding.guideline_id)
        .where(Guideline.agency_id == agency_id)
    )
    for emb, itype in result.all():
        if itype == item_type and emb.model == EMBED_MODEL_NAME:
            idx.add(emb.guideline_id, emb.title, unpack_vector(emb.vector))
    return idx


def make_embedding_row(guideline_id: int, title: str) -> tuple["object", list[float]]:
    """신규 가이드라인의 임베딩 행 생성 (호출측에서 db.add). 벡터도 반환(인덱스 추가용)."""
    from app.models.guideline_embedding import GuidelineEmbedding

    vec = encode_title(title)
    row = GuidelineEmbedding(
        guideline_id=guideline_id,
        model=EMBED_MODEL_NAME,
        dim=EMBED_DIM,
        title=title[:500],
        vector=pack_vector(vec),
    )
    return row, vec


# ── 통합 진입점 ──────────────────────────────────────────


def find_same_document_sync(
    idx: EmbeddingIndex, agency_name: str, title: str,
) -> int | None:
    """신규 제목이 기존 문서의 다른 판이면 해당 guideline_id 반환 (동기)."""
    for gid, cand_title, sim in idx.candidates(title):
        verdict = llm_same_document_sync(agency_name, title, cand_title)
        if verdict is True:
            logger.info(
                "[identity] 동일문서 판정: '%s' == g%d '%s' (sim=%.3f)",
                title[:40], gid, cand_title[:40], sim,
            )
            return gid
    return None


async def find_same_document(
    idx: EmbeddingIndex, agency_name: str, title: str,
) -> int | None:
    """신규 제목이 기존 문서의 다른 판이면 해당 guideline_id 반환 (비동기)."""
    for gid, cand_title, sim in idx.candidates(title):
        verdict = await llm_same_document(agency_name, title, cand_title)
        if verdict is True:
            logger.info(
                "[identity] 동일문서 판정: '%s' == g%d '%s' (sim=%.3f)",
                title[:40], gid, cand_title[:40], sim,
            )
            return gid
    return None
