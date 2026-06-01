"""
PDF 콘텐츠 해시 — 동일 문서 식별.

URL 식별자(cfIdx/seq)는 같은 파일이 다른 URL/기관으로 올라오면 못 잡는다.
실제 PDF 본문을 받아 SHA-256을 계산하면 출처와 무관하게 동일 문서를 탐지할 수 있다.

비용 제어:
- HEAD/ETag 미지원 사이트가 많아 GET 다운로드 필수
- 신규 버전 후보일 때만 호출 (이미 hash 보유 버전은 재계산 안 함)
- 1차 URL fingerprint(guideline_sync.content_fingerprint)로 걸러진 건 호출 안 함
- 다운로드 실패/비-PDF 응답이면 None 반환 → 호출 측은 hash 없이 진행 (graceful)
"""

import hashlib
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 60 * 1024 * 1024   # 60MB 상한 (비정상 대용량 방어)
DOWNLOAD_TIMEOUT = 30


async def fetch_pdf_sha256(url: str) -> str | None:
    """PDF URL을 다운로드하여 SHA-256(hex)을 반환. 실패 시 None.

    - Content-Type이 HTML이면 (다운로드 실패/로그인 페이지 등) None
    - 본문이 비어있거나 상한 초과 시 None
    """
    if not url:
        return None
    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT,
            headers={"User-Agent": settings.crawl_user_agent},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.debug("[pdf_hash] HTTP %s for %s", resp.status_code, url[:80])
                return None
            ctype = resp.headers.get("content-type", "").lower()
            content = resp.content
            # HTML 응답 = 실제 파일 아님 (WAF 차단, 로그인 페이지 등)
            if "text/html" in ctype:
                logger.debug("[pdf_hash] HTML 응답(파일 아님): %s", url[:80])
                return None
            if not content or len(content) > MAX_PDF_BYTES:
                return None
            # PDF magic number 확인 (선택적 — hwp 등도 허용하되 HTML만 배제)
            digest = hashlib.sha256(content).hexdigest()
            return digest
    except Exception as e:
        logger.debug("[pdf_hash] 다운로드 실패 %s: %s", url[:80], e)
        return None


def sha256_bytes(content: bytes) -> str:
    """바이트 직접 해시 (테스트/배치용)."""
    return hashlib.sha256(content).hexdigest()
