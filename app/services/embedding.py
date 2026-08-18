"""
제목 임베딩 서비스 — 의미 기반 정체성 판정(Tier 1)의 인코더.

모델: intfloat/multilingual-e5-small (384차원, 한국어 지원, 경량).
2026-08-18 캘리브레이션 (실데이터 제목 쌍):
  - 같은 문서 다른 판:   0.945 ~ 0.970
  - 유사한 다른 문서:    0.909 ~ 0.938
분리 폭이 좁으므로 임베딩 단독 자동 병합 금지 — 후보 생성(recall)에만 쓰고
최종 판정은 LLM(identity.py Tier 2)이 담당한다.

모델 로드는 프로세스당 1회(lazy). 짧은 제목 인코딩은 ~10ms 수준이라
async 컨텍스트에서 동기 호출해도 무방하다.
"""

import logging
import struct

logger = logging.getLogger(__name__)

EMBED_MODEL_NAME = "intfloat/multilingual-e5-small"
EMBED_DIM = 384

# 후보 생성 임계값 — 캘리브레이션상 SAME 최저 0.945에 여유를 둔 값.
# 이보다 낮으면 후보에서 제외 (LLM 호출 자체를 안 함)
CANDIDATE_SIM_THRESHOLD = 0.90

_model = None


def _get_model():
    """SentenceTransformer lazy 싱글턴 (MPS 우선, 실패 시 CPU)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        try:
            _model = SentenceTransformer(EMBED_MODEL_NAME, device="mps")
        except Exception as e:
            logger.warning("MPS 로드 실패, CPU로 폴백: %s", e)
            _model = SentenceTransformer(EMBED_MODEL_NAME, device="cpu")
        logger.info("임베딩 모델 로드 완료: %s", EMBED_MODEL_NAME)
    return _model


def encode_title(title: str) -> list[float]:
    """제목을 L2 정규화된 임베딩 벡터로 인코딩.

    e5 계열은 'query: ' 프리픽스 규약을 따른다 (양쪽 동일 프리픽스면 대칭 비교 가능).
    """
    model = _get_model()
    vec = model.encode([f"query: {title.strip()}"], normalize_embeddings=True)[0]
    return vec.tolist()


def pack_vector(vec: list[float]) -> bytes:
    """float32 리틀엔디언으로 직렬화 (DB LargeBinary 저장용)."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(data: bytes) -> list[float]:
    """pack_vector의 역변환."""
    n = len(data) // 4
    return list(struct.unpack(f"<{n}f", data))


def cosine(a: list[float], b: list[float]) -> float:
    """정규화된 벡터 간 코사인 유사도 (= 내적)."""
    return sum(x * y for x, y in zip(a, b))
