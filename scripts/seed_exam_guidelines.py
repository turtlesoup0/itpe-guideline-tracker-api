"""
기출문제에서 언급된 고빈도 가이드라인 중 크롤링 미수집 항목을 수동 시드합니다.

사용법:
    cd itpe-guideline-tracker-api
    source .venv/bin/activate
    python scripts/seed_exam_guidelines.py
"""

import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.db.session import async_session_factory
from app.models.guideline import Guideline, GuidelineVersion

# ── 기출 고빈도 미수집 가이드라인 시드 데이터 ─────────────
# agency_id: 1=PIPC, 2=MSIT, 3=KISA, 4=NIS, 5=FSC, 6=NIA, 7=MOIS, 8=SPRI, 9=KCC

EXAM_GUIDELINES = [
    # ── 🔴 최우선 (최신 기출 2~3교시 출제) ──
    {
        "agency_id": 9,  # KCC (방송통신위원회)
        "title": "생성형 인공지능 서비스 이용자 보호 가이드라인",
        "category": "ai",
        "description": "생성형 AI 서비스 이용자 보호를 위한 4가지 기본원칙 + 6가지 실행방식. 138회 관리 2교시 출제.",
        "source_url": "https://www.korea.kr/briefing/pressReleaseView.do?newsId=156676724",
        "published_date": date(2025, 2, 28),
        "exam_refs": "138회 관리 2교시",
    },
    {
        "agency_id": 6,  # NIA
        "title": "공공부문 초거대 AI 도입·활용 가이드라인 v2.0",
        "category": "ai",
        "description": "공공부문 초거대AI 도입 원칙, 사전 고려사항, 보안통제항목, RAG 기술 도입방식. 134회 응용 2교시, 137회 관리 출제.",
        "source_url": "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?cbIdx=99953&bcIdx=27985",
        "published_date": date(2025, 4, 16),
        "exam_refs": "134회 응용 2교시, 137회 관리",
    },
    {
        "agency_id": 8,  # SPRI (실질 발행: KOSA이나 SPRi 연구소 소속으로 분류)
        "title": "SW사업 대가산정 가이드 (2025년 개정판)",
        "category": "software",
        "description": "SW 사업 대가산정 방법 비교, AI 도입사업 대가체계, DevOps 대가기준 신설. 117~137회 거의 매 회차 출제 단골 주제.",
        "source_url": "https://www.sw.or.kr/site/sw/ex/board/View.do?cbIdx=276&bcIdx=63607",
        "published_date": date(2025, 8, 11),
        "exam_refs": "117~137회 다수 출제",
    },
    {
        "agency_id": 4,  # NIS
        "title": "국가 망 보안체계(N²SF) 보안가이드라인 1.0",
        "category": "info_security",
        "description": "국가 망 보안 체계 N2SF 보안통제항목 260여개, 정보서비스 모델 11개. 135회 응용, 137회 관리 출제.",
        "source_url": "https://www.ncsc.go.kr:4018/main/cop/bbs/selectBoardArticle.do?bbsId=Notification_main&nttId=218022",
        "published_date": date(2025, 9, 9),
        "exam_refs": "135회 응용, 137회 관리",
    },
    # ── 🟠 고우선 ──
    {
        "agency_id": 6,  # NIA → TTA 주관이지만 NIA 카테고리
        "title": "AI 신뢰성 검인증(CAT 2.0) 가이드",
        "category": "ai",
        "description": "ISO/IEC 23894, 42001 기반 AI 신뢰성 인증 체계. 기능/성능 시험 강화, 기업규모별 차등 적용. 133회, 137회 관리 출제.",
        "source_url": "https://tta-trustworthy-ai.gitbook.io/cat/cat-2.0/aisystem",
        "published_date": date(2025, 4, 1),
        "exam_refs": "133회 관리 1교시, 137회 관리",
    },
    {
        "agency_id": 2,  # MSIT
        "title": "인공지능 윤리기준 (3대 기본원칙 + 10대 핵심요건)",
        "category": "ai",
        "description": "인간 존엄성·사회 공공선·기술 합목적성 3대 원칙, 인권보장~투명성 10대 요건. 129~136회 다수 출제.",
        "source_url": "https://www.msit.go.kr/bbs/view.do?sCode=user&mPid=112&mId=113&bbsSeqNo=94&nttSeqNo=3179742",
        "published_date": date(2020, 12, 23),
        "exam_refs": "129회 관리, 131회 관리, 136회 관리",
    },
    {
        "agency_id": 6,  # NIA
        "title": "정보시스템 운영 및 유지보수 감리 점검 가이드 Ver.2.0",
        "category": "e_gov",
        "description": "정보시스템 감리기준(행안부 고시) 제24조 근거. 운영 16개 + 유지보수 34개 점검분야. 137회 관리 출제.",
        "source_url": "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?cbIdx=99860&bcIdx=19572",
        "published_date": date(2022, 1, 1),
        "exam_refs": "137회 관리",
    },
    {
        "agency_id": 2,  # MSIT
        "title": "소프트웨어사업 영향평가 가이드라인",
        "category": "software",
        "description": "소프트웨어진흥법 제43조 근거. 대상기관, 평가체계, 평가항목 규정. 128회 응용 1교시, 132회 응용, 137회 응용 출제.",
        "source_url": "https://www.nipa.kr/home/2-8/8899",
        "published_date": date(2021, 12, 1),
        "exam_refs": "128회 응용, 132회 응용, 137회 응용",
    },
    # ── 🟠 국제 표준이지만 기출 고빈도 ──
    {
        "agency_id": 3,  # KISA (보안 분야 국제 기준)
        "title": "OWASP Top 10 for LLM Applications 2025",
        "category": "info_security",
        "description": "LLM 애플리케이션 보안 취약점 Top 10 (Prompt Injection ~ Unbounded Consumption). 136회 관리, 137회 응용 출제.",
        "source_url": "https://genai.owasp.org/resource/owasp-top-10-for-llm-applications-2025/",
        "published_date": date(2025, 1, 1),
        "exam_refs": "136회 관리, 137회 응용",
    },
    # ── 🟡 중요도 보통이지만 기출 반복 ──
    {
        "agency_id": 3,  # KISA
        "title": "클라우드 보안인증제도(CSAP) 가이드",
        "category": "cloud",
        "description": "공공부문 민간 클라우드 서비스 보안인증 절차, SaaS/PaaS/IaaS 평가기준. 128회 관리, 129회 응용, 134회 응용 출제.",
        "source_url": "https://isms.kisa.or.kr/main/csap/intro/",
        "published_date": date(2024, 1, 1),
        "exam_refs": "128회 관리, 129회 응용, 134회 응용",
    },
    {
        "agency_id": 6,  # NIA
        "title": "지능정보기술 감리 실무 가이드",
        "category": "ai",
        "description": "빅데이터, AI, 클라우드 등 지능정보기술 사업의 단계별 감리 점검항목. 130회 관리, 134회 응용 출제.",
        "source_url": "https://www.nia.or.kr/site/nia_kor/ex/bbs/List.do?cbIdx=99860",
        "published_date": date(2023, 2, 1),
        "exam_refs": "130회 관리, 134회 응용",
    },
]


async def main():
    async with async_session_factory() as db:
        created = 0
        skipped = 0

        for item in EXAM_GUIDELINES:
            # 중복 체크: 제목 유사 매칭
            existing = await db.execute(
                select(Guideline).where(
                    Guideline.agency_id == item["agency_id"],
                    Guideline.title.ilike(f"%{item['title'][:30]}%"),
                )
            )
            if existing.scalars().first():
                print(f"  SKIP (exists): {item['title'][:60]}")
                skipped += 1
                continue

            # Guideline 생성
            guideline = Guideline(
                agency_id=item["agency_id"],
                title=item["title"],
                category=item["category"],
                description=item["description"],
                source_url=item["source_url"],
            )
            db.add(guideline)
            await db.flush()  # ID 확보

            # GuidelineVersion 생성
            version = GuidelineVersion(
                guideline_id=guideline.id,
                version_label=None,
                published_date=item["published_date"],
                detected_at=datetime.now(),
                change_summary=f"기출 참조 수동 시드 ({item['exam_refs']})",
            )
            db.add(version)
            print(f"  CREATE: [{guideline.id}] {item['title'][:60]}")
            created += 1

        await db.commit()
        print(f"\nDone: {created} created, {skipped} skipped")

        # 최종 카운트
        total = await db.execute(select(Guideline))
        print(f"Total guidelines: {len(total.scalars().all())}")


if __name__ == "__main__":
    asyncio.run(main())
