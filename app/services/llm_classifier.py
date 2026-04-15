"""
로컬 Gemma 모델을 이용한 IT 가이드라인 분류기.

Ollama(localhost)에서 gemma4:26b 모델을 사용하여
제목 + 게시판명 + 본문 스니펫으로 가이드라인 여부를 판별합니다.

Stage 1-2(정규식)에서 판단 불가한 경계 케이스에만 호출됩니다.
"""

import logging
import re
from typing import NamedTuple

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Ollama 직접 포트 (nginx 프록시 우회)
OLLAMA_BASE_URL = "http://localhost:41434"
OLLAMA_MODEL = "gemma4:26b"
OLLAMA_TIMEOUT = 30.0

CLASSIFY_PROMPT = """\
다음 게시물이 IT/정보보안/개인정보/SW 분야의 \
가이드라인·지침·안내서·표준·매뉴얼·해설서·프레임워크·백서·로드맵·권고 문서인지 판단하세요.

제목: {title}
게시판: {board_label}
기관: {agency_name}
{body_section}

판단 기준:
- 실제 문서(PDF/HWP) 형태로 배포되는 기술 지침·기준이면 YES
- 보도자료, 행사 안내, 뉴스, 채용, 홍보성 글이면 NO
- 비-IT 분야(인사, 세금, 부동산, 복지 등)면 NO
- 법령 개정 소식만 전하는 글이면 NO (법령 자체의 해설서는 YES)

YES 또는 NO만 답하세요."""


class ClassifyResult(NamedTuple):
    """분류 결과."""
    is_guideline: bool
    confidence: str    # "high" (Stage 1-2) | "llm" (Stage 3)
    reason: str        # 판단 근거 요약


async def fetch_body_snippet(url: str, max_chars: int = 500) -> str:
    """상세 페이지에서 본문 텍스트 스니펫을 추출합니다.

    크롤링 부하를 최소화하기 위해 짧은 타임아웃 + 텍스트만 추출.
    실패 시 빈 문자열 반환 (Stage 3 판정은 제목+게시판명만으로도 가능).
    """
    if not url:
        return ""

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=True,
            headers={"User-Agent": "GuidelineTracker/1.0"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.debug("상세 페이지 fetch 실패 (%s): %s", url[:80], e)
        return ""

    try:
        soup = BeautifulSoup(resp.text, "lxml")

        # 불필요한 태그 제거
        for tag in soup.select("script, style, nav, header, footer, .gnb, .lnb"):
            tag.decompose()

        # 본문 영역 후보 (정부 사이트 공통 패턴)
        content_area = (
            soup.select_one(".board_view_con")
            or soup.select_one(".bbs_view_con")
            or soup.select_one(".view_con")
            or soup.select_one(".board_view")
            or soup.select_one(".bbs_detail")
            or soup.select_one("#contents")
            or soup.select_one("article")
            or soup.select_one("main")
        )

        if content_area:
            text = content_area.get_text(separator=" ", strip=True)
        else:
            # 폴백: body 전체에서 텍스트 추출
            text = soup.body.get_text(separator=" ", strip=True) if soup.body else ""

        # 공백 정리 + 길이 제한
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]

    except Exception as e:
        logger.debug("HTML 파싱 실패 (%s): %s", url[:80], e)
        return ""


def _extract_attachment_names(body_snippet: str) -> list[str]:
    """본문 스니펫에서 첨부파일명 패턴을 추출합니다."""
    patterns = re.findall(
        r"[\w가-힣\-_]+\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?)",
        body_snippet,
        re.IGNORECASE,
    )
    return [f".{ext}" for ext in patterns]


async def classify_with_llm(
    title: str,
    board_label: str,
    agency_name: str,
    detail_url: str = "",
) -> ClassifyResult:
    """Ollama Gemma 모델로 가이드라인 여부를 분류합니다.

    Args:
        title: 게시물 제목
        board_label: 크롤링 대상 게시판 이름 (CrawlConfig.label)
        agency_name: 기관명
        detail_url: 상세 페이지 URL (본문 스니펫 추출용)

    Returns:
        ClassifyResult(is_guideline, confidence="llm", reason)
    """
    # 1) 상세 페이지에서 본문 스니펫 + 첨부파일 정보 추출
    body_snippet = await fetch_body_snippet(detail_url)
    attachments = _extract_attachment_names(body_snippet)

    # 2) 본문 섹션 구성
    body_parts = []
    if body_snippet:
        body_parts.append(f"본문 첫 500자: {body_snippet}")
    if attachments:
        body_parts.append(f"첨부파일 확장자: {', '.join(attachments)}")
    body_section = "\n".join(body_parts) if body_parts else "본문: (추출 불가)"

    # 3) LLM 호출
    prompt = CLASSIFY_PROMPT.format(
        title=title,
        board_label=board_label,
        agency_name=agency_name,
        body_section=body_section,
    )

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(OLLAMA_TIMEOUT)) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0, "num_predict": 10},
                },
            )
            resp.raise_for_status()
            data = resp.json()

    except Exception as e:
        logger.warning("Ollama 호출 실패: %s — 보수적으로 제외", e)
        return ClassifyResult(
            is_guideline=False,
            confidence="llm_error",
            reason=f"Ollama 호출 실패: {e}",
        )

    answer = data.get("message", {}).get("content", "").strip().upper()
    is_yes = answer.startswith("YES")

    reason_parts = [f"LLM={answer}"]
    if attachments:
        reason_parts.append(f"첨부={','.join(attachments)}")
    if not body_snippet:
        reason_parts.append("본문추출실패")

    return ClassifyResult(
        is_guideline=is_yes,
        confidence="llm",
        reason=" | ".join(reason_parts),
    )
