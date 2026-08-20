"""수집 제외 API 재현/회귀 테스트.

버그: POST /guidelines/{id}/exclude 가 500 (sqlalchemy MissingGreenlet).
     commit() 으로 인스턴스가 만료된 뒤 refresh(["versions"]) 로 versions 만
     되살리고 나머지 컬럼을 getattr 로 읽어, 응답 직렬화 중 지연 로딩 IO가
     async 컨텍스트 밖에서 발생했다.

재현 조건: 임시 Guideline 하나를 만들고 exclude → restore 를 호출.
로컬 postgres(guideline_tracker)에 직접 붙는다 — 테스트가 만든 행은 끝에 지운다.
"""

import httpx
import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.main import app
from app.models.agency import Agency
from app.models.crawl_decision import CrawlDecision, DecisionOutcome
from app.models.guideline import ExclusionCategory, Guideline
from app.tasks.crawl_tasks import _run_async

TEST_URL = "https://example.test/exclusion-regression-fixture"


async def _create_fixture() -> int:
    async with async_session_factory() as session:
        agency_id = (await session.execute(select(Agency.id).limit(1))).scalar_one()
        guideline = Guideline(
            agency_id=agency_id,
            title="[테스트] 수집 제외 회귀 픽스처",
            source_url=TEST_URL,
        )
        session.add(guideline)
        await session.commit()
        return guideline.id


async def _cleanup(guideline_id: int) -> None:
    async with async_session_factory() as session:
        await session.execute(delete(CrawlDecision).where(CrawlDecision.url == TEST_URL))
        await session.execute(delete(Guideline).where(Guideline.id == guideline_id))
        await session.commit()


async def _decision() -> CrawlDecision | None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(CrawlDecision).where(CrawlDecision.url == TEST_URL)
        )
        return result.scalar_one_or_none()


async def _exclude_restore_flow(guideline_id: int) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 제외 — 버그 시 여기서 500
        resp = await client.post(
            f"/guidelines/{guideline_id}/exclude",
            json={"category": "physical_security", "note": "회귀 테스트"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["excluded_at"] is not None
        assert body["exclusion_category"] == ExclusionCategory.PHYSICAL_SECURITY.value
        assert body["exclusion_note"] == "회귀 테스트"

        # 기본 목록에서 빠지고, 제외함에서 보여야 한다
        active = (await client.get("/guidelines")).json()
        assert all(g["id"] != guideline_id for g in active)
        excluded = (await client.get("/guidelines", params={"excluded": True})).json()
        assert any(g["id"] == guideline_id for g in excluded)

        # 재수집 차단용 판정 기록
        decision = await _decision()
        assert decision is not None
        assert decision.outcome == DecisionOutcome.EXCLUDED
        assert decision.stage == "manual"

        # 복구
        resp = await client.post(f"/guidelines/{guideline_id}/restore")
        assert resp.status_code == 200, resp.text
        assert resp.json()["excluded_at"] is None
        assert await _decision() is None

        # 분류가 '기타'인데 메모가 없으면 거부
        resp = await client.post(
            f"/guidelines/{guideline_id}/exclude", json={"category": "other"}
        )
        assert resp.status_code == 422, resp.text


def test_exclude_and_restore_roundtrip():
    guideline_id = _run_async(_create_fixture())
    try:
        _run_async(_exclude_restore_flow(guideline_id))
    finally:
        _run_async(_cleanup(guideline_id))
