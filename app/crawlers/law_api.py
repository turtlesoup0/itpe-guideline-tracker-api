"""
법제처 행정규칙 크롤러 — DRF JSON API 사용.

국가법령정보센터 DRF API에서 기관별 고시/훈령/예규를 검색하여
법적 근거(LegalBasis) 데이터를 수집합니다.

API 엔드포인트:
  http://www.law.go.kr/DRF/lawSearch.do?OC=itpe_law_follower&target=admrul&type=JSON&query=...

응답 구조:
  { "AdmRulSearch": { "totalCnt": N, "admrul": [ { "행정규칙명": ..., ... } ] } }
"""

from datetime import date
from dataclasses import dataclass

import httpx

from app.config import get_settings


@dataclass
class AdminRuleItem:
    """행정규칙 검색 결과 항목."""
    title: str
    rule_type: str           # 고시, 훈령, 예규
    agency_name: str         # 발행기관 (소관부처명)
    promulgation_date: date | None
    enforcement_date: date | None
    law_api_id: str          # 행정규칙일련번호
    source_url: str


# ── 기관별 법제처 검색 키워드 매핑 ─────────────────────────

AGENCY_SEARCH_TERMS: dict[str, list[str]] = {
    "PIPC": ["개인정보보호위원회"],
    "MSIT": ["과학기술정보통신부"],
    "KISA": ["한국인터넷진흥원"],
    "NIS": ["국가정보원"],
    "FSC": ["금융위원회"],
    "NIA": ["한국지능정보사회진흥원"],
    "MOIS": ["행정안전부"],
    "KCC": ["방송통신위원회"],
}

# IT/보안 관련 키워드 (검색 결과 필터링)
# "지침", "기준", "가이드"는 범용적이라 모든 행정규칙에 매칭되므로 제외
RELEVANCE_KEYWORDS = [
    "정보보호", "정보보안", "개인정보", "사이버", "보안",
    "소프트웨어", "전자정부", "클라우드", "인공지능", "데이터",
    "전자금융", "핀테크", "암호", "인증", "접근통제",
    "정보통신", "전자서명", "전자문서", "망분리", "정보시스템",
    "주요정보통신기반", "통신비밀", "스팸", "이용자보호",
]

# DRF API 설정
DRF_API_URL = "http://www.law.go.kr/DRF/lawSearch.do"
DRF_OC_KEY = "itpe_law_follower"

# 행정규칙종류 → 한국어 매핑
RULE_TYPE_MAP: dict[str, str] = {
    "고시": "고시",
    "훈령": "훈령",
    "예규": "예규",
    "규칙": "고시",  # fallback
}


def _parse_date_yyyymmdd(s: str | None) -> date | None:
    """YYYYMMDD 형식 문자열을 date로 변환."""
    if not s or len(s) < 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, IndexError):
        return None


async def search_admin_rules(
    agency_code: str,
    max_results: int = 30,
) -> list[AdminRuleItem]:
    """법제처 DRF API에서 기관별 행정규칙(고시/훈령/예규)을 검색합니다.

    DRF API를 사용하여 JSON 응답을 파싱합니다.
    소관부처명으로 필터링하고, RELEVANCE_KEYWORDS로 IT/보안 관련성을 체크합니다.
    """
    settings = get_settings()
    search_terms = AGENCY_SEARCH_TERMS.get(agency_code, [])
    if not search_terms:
        return []

    items: list[AdminRuleItem] = []
    seen_ids: set[str] = set()  # 중복 방지

    async with httpx.AsyncClient(
        headers={"User-Agent": settings.crawl_user_agent},
        follow_redirects=True,
        timeout=30,
    ) as client:
        for term in search_terms:
            try:
                import asyncio
                await asyncio.sleep(settings.crawl_request_delay_sec)

                # DRF API 호출 — 소관부처명으로 검색
                params = {
                    "OC": DRF_OC_KEY,
                    "target": "admrul",
                    "type": "JSON",
                    "query": term,
                    "display": str(min(max_results * 2, 100)),  # 필터링 후 줄어들므로 넉넉히
                }

                res = await client.get(DRF_API_URL, params=params)
                if not res.is_success:
                    raise RuntimeError(f"DRF API HTTP {res.status_code} for '{term}'")

                data = res.json()

                # API 인증 실패 탐지 — "사용자 정보 검증 실패" 등
                if "result" in data and "검증에 실패" in str(data.get("result", "")):
                    raise RuntimeError(
                        f"법제처 DRF API 인증 실패 (OC 키 '{DRF_OC_KEY}'의 등록 IP와 "
                        f"서버 IP 불일치). 법제처 OPEN API 페이지에서 현재 서버 IP 등록 필요."
                    )

                # 응답 구조: { "AdmRulSearch": { "totalCnt": N, "admrul": [...] } }
                search_result = data.get("AdmRulSearch", {})
                rules_list = search_result.get("admrul", [])

                # 단건 결과일 때 dict로 오는 경우 처리
                if isinstance(rules_list, dict):
                    rules_list = [rules_list]

                for rule in rules_list:
                    title = rule.get("행정규칙명", "").strip()
                    if not title:
                        continue

                    # IT/보안 관련성 체크 (기관명 제거 후 — 기관명 자체에 키워드 포함 가능)
                    title_for_check = title
                    for agency_term in search_terms:
                        title_for_check = title_for_check.replace(agency_term, "")
                    if not any(kw in title_for_check for kw in RELEVANCE_KEYWORDS):
                        continue

                    # 중복 체크 (행정규칙일련번호 기준)
                    rule_serial = str(rule.get("행정규칙일련번호", ""))
                    if rule_serial in seen_ids:
                        continue
                    if rule_serial:
                        seen_ids.add(rule_serial)

                    # 행정규칙종류 파싱
                    rule_type_raw = rule.get("행정규칙종류", "고시").strip()
                    rule_type = RULE_TYPE_MAP.get(rule_type_raw, rule_type_raw)

                    # 날짜 파싱 (YYYYMMDD)
                    prom_date = _parse_date_yyyymmdd(rule.get("발령일자"))
                    enf_date = _parse_date_yyyymmdd(rule.get("시행일자"))

                    # 상세 링크 구성
                    detail_link = rule.get("행정규칙상세링크", "")
                    if detail_link and not detail_link.startswith("http"):
                        source_url = f"https://www.law.go.kr{detail_link}"
                    elif detail_link:
                        source_url = detail_link
                    else:
                        source_url = f"https://www.law.go.kr/행정규칙/{title}"

                    items.append(AdminRuleItem(
                        title=title,
                        rule_type=rule_type,
                        agency_name=rule.get("소관부처명", term).strip(),
                        promulgation_date=prom_date,
                        enforcement_date=enf_date,
                        law_api_id=rule_serial,
                        source_url=source_url,
                    ))

                    if len(items) >= max_results:
                        break

            except RuntimeError:
                # 인증 실패 등 — 다음 term도 어차피 실패이므로 즉시 전파
                raise
            except Exception as e:
                print(f"[law_api] search_admin_rules error for {term}: {e}")
                continue

            if len(items) >= max_results:
                break

    return items


async def crawl_admin_rules(agency_code: str) -> "CrawlResult":
    """법제처 API 크롤링 결과를 CrawlResult(CrawledItem) 형태로 반환합니다.

    기존 BBS/RSS 크롤러와 동일한 파이프라인(sync_crawl_results)에
    투입될 수 있도록 CrawledItem으로 변환합니다.
    """
    from datetime import datetime
    from app.crawlers.base import CrawledItem, CrawlResult

    started = datetime.now()

    try:
        rules = await search_admin_rules(agency_code, max_results=50)
    except Exception as e:
        return CrawlResult(
            agency_code=agency_code,
            config_label="법제처 행정규칙",
            started_at=started,
            finished_at=datetime.now(),
            error=str(e),
        )

    items = [
        CrawledItem(
            title=rule.title,
            url=rule.source_url,
            published_date=rule.promulgation_date or rule.enforcement_date,
        )
        for rule in rules
    ]

    return CrawlResult(
        agency_code=agency_code,
        config_label="법제처 행정규칙",
        started_at=started,
        finished_at=datetime.now(),
        items=items,
    )


async def fetch_and_store_legal_bases(
    agency_code: str,
    db_session: object,
) -> dict:
    """기관의 행정규칙을 DRF API로 검색하여 DB에 저장합니다.

    이미 존재하는 항목(law_api_id 기준)은 건너뜁니다.
    """
    from sqlalchemy import select
    from app.models.agency import Agency
    from app.models.guideline import LegalBasis, LegalBasisType

    # Agency 조회
    result = await db_session.execute(
        select(Agency).where(Agency.code == agency_code)
    )
    agency = result.scalar_one_or_none()
    if not agency:
        return {"error": f"Agency {agency_code} not found"}

    # DRF API에서 행정규칙 검색
    rules = await search_admin_rules(agency_code)

    created = 0
    skipped = 0

    type_map = {
        "고시": LegalBasisType.GOSI,
        "훈령": LegalBasisType.HUNRYEONG,
        "예규": LegalBasisType.YEGYU,
    }

    for rule in rules:
        # 중복 체크
        if rule.law_api_id:
            existing = await db_session.execute(
                select(LegalBasis).where(LegalBasis.law_api_id == rule.law_api_id)
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue

        basis = LegalBasis(
            agency_id=agency.id,
            basis_type=type_map.get(rule.rule_type, LegalBasisType.GOSI),
            title=rule.title,
            law_api_id=rule.law_api_id or None,
            promulgation_date=rule.promulgation_date,
            enforcement_date=rule.enforcement_date,
            source_url=rule.source_url,
        )
        db_session.add(basis)
        created += 1

    return {
        "agency": agency_code,
        "searched": len(rules),
        "created": created,
        "skipped": skipped,
    }
