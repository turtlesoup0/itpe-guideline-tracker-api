"""
로컬 Gemma 모델을 이용한 IT 가이드라인 분류기.

로컬 MLX 서버(mlx_lm server, OpenAI 호환 API)의 supergemma4-26b 모델로
제목 + 게시판명 + 본문 스니펫으로 가이드라인 여부를 판별합니다.
(2026-08-18: Ollama 제거됨 → itpe-topic-splitter의 MLX 서버(port 8090)로 전환.
 서버는 launchd com.itpe.splitter.mlx 가 상시 구동.)

Stage 1-2(정규식)에서 판단 불가한 경계 케이스에만 호출됩니다.
"""

import logging
import re
from typing import NamedTuple

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 로컬 MLX 서버 (OpenAI 호환) — com.itpe.splitter.mlx launchd 서비스
LLM_BASE_URL = "http://127.0.0.1:8090/v1"
LLM_MODEL = "Jiunsong/supergemma4-26b-uncensored-mlx-4bit-v2"
LLM_TIMEOUT = 60.0

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
- 추진계획, 종합계획, 업무계획 등 정책 계획서면 NO
- 선정제품 목록, 평가결과 등 단순 리스트면 NO
- 교육교재, 교육과정, 인식제고 자료면 NO
- 운영방안, 사업계획, 수립결과 등 내부 운영문서면 NO

YES 또는 NO만 답하세요."""

# announcement 소스(보도자료·공지 게시판) 전용 프롬프트.
# 목적이 다르다: 실제 문서 여부가 아니라, IT 규범(가이드라인·고시·법령)의
# 제·개정/발간 "발표"인지를 판정한다.
ANNOUNCEMENT_CLASSIFY_PROMPT = """\
다음 보도자료/공지 게시물이 IT/정보보안/개인정보/SW 분야의 \
가이드라인·지침·안내서·표준·고시·훈령·법령의 제정/개정/폐지/발간/배포 \
발표에 관한 것인지 판단하세요.

제목: {title}
게시판: {board_label}
기관: {agency_name}
{body_section}

판단 기준:
- 가이드라인·지침·표준·고시·법령의 제·개정·발간·배포를 알리는 글이면 YES
- 정책 방안·대책 발표로 후속 기준·지침 수립이 명시된 글이면 YES
- 행사·세미나·채용·수상·MOU·홍보·통계 발표면 NO
- 비-IT 분야(인사, 세금, 부동산, 복지, 일반 금융규제 등)면 NO
- 단속·제재·조사 결과 발표면 NO

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

    return _snippet_from_html(resp.text, url, max_chars)


def fetch_body_snippet_sync(url: str, max_chars: int = 500) -> str:
    """fetch_body_snippet의 동기 버전 (Celery 워커용)."""
    if not url:
        return ""

    try:
        with httpx.Client(
            timeout=httpx.Timeout(10.0),
            follow_redirects=True,
            headers={"User-Agent": "GuidelineTracker/1.0"},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.debug("상세 페이지 fetch 실패 (%s): %s", url[:80], e)
        return ""

    return _snippet_from_html(resp.text, url, max_chars)


def _snippet_from_html(html: str, url: str, max_chars: int) -> str:
    """HTML에서 본문 텍스트 스니펫 추출 (async/sync 공용)."""
    try:
        soup = BeautifulSoup(html, "lxml")

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


def _build_prompt(
    mode: str,
    title: str,
    board_label: str,
    agency_name: str,
    body_snippet: str,
    attachments: list[str],
) -> str:
    """모드별 프롬프트 구성. mode: "guideline" | "announcement"."""
    body_parts = []
    if body_snippet:
        body_parts.append(f"본문 첫 500자: {body_snippet}")
    if attachments:
        body_parts.append(f"첨부파일 확장자: {', '.join(attachments)}")
    body_section = "\n".join(body_parts) if body_parts else "본문: (추출 불가)"

    template = (
        ANNOUNCEMENT_CLASSIFY_PROMPT if mode == "announcement" else CLASSIFY_PROMPT
    )
    return template.format(
        title=title,
        board_label=board_label,
        agency_name=agency_name,
        body_section=body_section,
    )


def _chat_payload(prompt: str) -> dict:
    """OpenAI 호환 chat.completions 요청 본문 (MLX 서버용)."""
    return {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 10,
        # supergemma4는 reasoning 모델 — thinking을 끄지 않으면
        # max_tokens가 사고 토큰으로 소진되어 content가 비어버림
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _parse_response(
    data: dict, body_snippet: str, attachments: list[str],
) -> ClassifyResult:
    choices = data.get("choices") or [{}]
    answer = (choices[0].get("message", {}).get("content") or "").strip().upper()
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


def _error_result(e: Exception) -> ClassifyResult:
    logger.warning("로컬 LLM 호출 실패: %s", e)
    return ClassifyResult(
        is_guideline=False,
        confidence="llm_error",
        reason=f"로컬 LLM 호출 실패: {e}",
    )


async def classify_with_llm(
    title: str,
    board_label: str,
    agency_name: str,
    detail_url: str = "",
    mode: str = "guideline",
) -> ClassifyResult:
    """Ollama Gemma 모델로 가이드라인 여부를 분류합니다.

    Args:
        title: 게시물 제목
        board_label: 크롤링 대상 게시판 이름 (CrawlConfig.label)
        agency_name: 기관명
        detail_url: 상세 페이지 URL (본문 스니펫 추출용)
        mode: "guideline"(실제 문서 여부) | "announcement"(규범 발표 여부)

    Returns:
        ClassifyResult(is_guideline, confidence="llm"|"llm_error", reason)
    """
    body_snippet = await fetch_body_snippet(detail_url)
    attachments = _extract_attachment_names(body_snippet)
    prompt = _build_prompt(mode, title, board_label, agency_name, body_snippet, attachments)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(LLM_TIMEOUT)) as client:
            resp = await client.post(f"{LLM_BASE_URL}/chat/completions", json=_chat_payload(prompt))
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return _error_result(e)

    return _parse_response(data, body_snippet, attachments)


def classify_with_llm_sync(
    title: str,
    board_label: str,
    agency_name: str,
    detail_url: str = "",
    mode: str = "guideline",
) -> ClassifyResult:
    """classify_with_llm의 동기 버전 — Celery 워커(동기 컨텍스트)용."""
    body_snippet = fetch_body_snippet_sync(detail_url)
    attachments = _extract_attachment_names(body_snippet)
    prompt = _build_prompt(mode, title, board_label, agency_name, body_snippet, attachments)

    try:
        with httpx.Client(timeout=httpx.Timeout(LLM_TIMEOUT)) as client:
            resp = client.post(f"{LLM_BASE_URL}/chat/completions", json=_chat_payload(prompt))
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return _error_result(e)

    return _parse_response(data, body_snippet, attachments)
