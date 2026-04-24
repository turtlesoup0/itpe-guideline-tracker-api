"""
Celery 크롤링 태스크.

Beat 스케줄에 의해 주기적으로 실행되거나,
API에서 수동 트리거할 수 있습니다.
"""

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.tasks.celery_app import celery
from app.crawlers.base import CrawlResult
from app.crawlers.bbs import BbsCrawler
from app.crawlers.rss import RssCrawler
from app.models.agency import (
    Agency,
    CrawlConfig,
    CrawlRun,
    CrawlRunStatus,
    CrawlSchedule,
    CrawlSourceType,
)

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────


def _get_sync_session():
    """Celery 워커용 동기 세션 팩토리.

    Celery 태스크는 자체 이벤트 루프에서 실행되므로
    동기 SQLAlchemy 세션을 사용합니다.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker
    from app.config import get_settings

    settings = get_settings()
    # asyncpg → psycopg2 변환 (동기 드라이버)
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2").replace("postgresql+psycopg2", "postgresql")
    engine = create_engine(sync_url, pool_pre_ping=True)
    return sessionmaker(engine, class_=Session)()


async def _run_crawl_config(config: CrawlConfig, agency_code: str) -> CrawlResult:
    """CrawlConfig에 따라 적절한 크롤러를 실행합니다."""
    keyword_list = config.keyword_filter.split(",") if config.keyword_filter else []

    if config.source_type == CrawlSourceType.RSS:
        crawler = RssCrawler(
            agency_code=agency_code,
            feed_url=config.url,
            keyword_filter=keyword_list,
            config_label=config.label,
        )
    elif config.source_type == CrawlSourceType.BBS_LIST:
        crawler = BbsCrawler(
            agency_code=agency_code,
            base_url=config.url,
            list_selector=config.list_selector,
            title_selector=config.title_selector,
            date_selector=config.date_selector,
            link_selector=config.link_selector,
            pagination_param=config.pagination_param,
            max_pages=config.max_pages,
            keyword_filter=keyword_list,
            config_label=config.label,
        )
    else:
        return CrawlResult(
            agency_code=agency_code,
            config_label=config.label,
            started_at=datetime.now(),
            finished_at=datetime.now(),
            error=f"Unsupported source type: {config.source_type}",
        )

    async with crawler:
        return await crawler.crawl()


def _run_async(coro):
    """Celery 태스크 내에서 async 코루틴을 실행합니다."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Tasks ────────────────────────────────────────────────


@celery.task(name="app.tasks.crawl_tasks.crawl_by_schedule")
def crawl_by_schedule(schedule: str) -> dict:
    """지정된 주기(daily/weekly/monthly)에 해당하는 크롤링 설정을 모두 실행합니다.

    Celery Beat에 의해 호출됩니다.
    """
    logger.info(f"[crawl] Starting {schedule} crawl")
    db = _get_sync_session()

    try:
        target_schedule = CrawlSchedule(schedule)

        # 해당 주기 + 활성 상태인 크롤링 설정 조회
        configs = (
            db.query(CrawlConfig)
            .join(Agency)
            .filter(CrawlConfig.schedule == target_schedule, CrawlConfig.is_active == True)
            .options(selectinload(CrawlConfig.agency))
            .all()
        )

        if not configs:
            logger.info(f"[crawl] No active {schedule} configs found")
            return {"schedule": schedule, "configs_run": 0, "total_items": 0}

        total_items = 0
        configs_run = 0
        results_summary: list[dict] = []

        for config in configs:
            agency_code = config.agency.code
            logger.info(f"[crawl] Running {agency_code}/{config.label}")

            try:
                result: CrawlResult = _run_async(_run_crawl_config(config, agency_code))

                # 크롤 성공 시 sync (동기 세션 기반)
                sync_stats = {"new": 0, "updated": 0, "skipped": 0}
                if result.success and result.items:
                    try:
                        from app.services.guideline_sync import sync_crawl_results_sync
                        sync_stats = sync_crawl_results_sync(
                            agency_id=config.agency_id,
                            items=result.items,
                            db=db,
                            config_label=config.label,
                            agency_name=config.agency.name,
                            config_item_type=config.item_type,
                        )
                    except Exception as se:
                        logger.warning(f"[sync] {agency_code}/{config.label} sync 실패: {se}")

                # DB에 실행 이력 저장
                run = CrawlRun(
                    agency_id=config.agency_id,
                    config_id=config.id,
                    status=CrawlRunStatus.SUCCESS if result.success else CrawlRunStatus.FAILED,
                    started_at=result.started_at,
                    finished_at=result.finished_at or datetime.now(),
                    items_found=result.count,
                    items_new=sync_stats["new"] + sync_stats["updated"],
                    error_message=result.error,
                )
                db.add(run)
                db.commit()

                total_items += result.count
                configs_run += 1

                results_summary.append({
                    "agency": agency_code,
                    "config": config.label,
                    "success": result.success,
                    "items": result.count,
                    "new": sync_stats["new"],
                    "updated": sync_stats["updated"],
                    "error": result.error,
                })

                logger.info(
                    f"[crawl] {agency_code}/{config.label}: "
                    f"{'OK' if result.success else 'FAIL'} ({result.count} items)"
                )

            except Exception as e:
                logger.error(f"[crawl] {agency_code}/{config.label} error: {e}")
                run = CrawlRun(
                    agency_id=config.agency_id,
                    config_id=config.id,
                    status=CrawlRunStatus.FAILED,
                    started_at=datetime.now(),
                    finished_at=datetime.now(),
                    error_message=str(e),
                )
                db.add(run)
                db.commit()

                results_summary.append({
                    "agency": agency_code,
                    "config": config.label,
                    "success": False,
                    "items": 0,
                    "error": str(e),
                })

        # 크롤링 완료 후 shared manifest 갱신
        try:
            from app.services.manifest import regenerate_manifest_sync
            manifest_result = regenerate_manifest_sync(db)
            logger.info(f"[crawl] Manifest 갱신: {manifest_result}")
        except Exception as e:
            logger.warning(f"[crawl] Manifest 갱신 실패 (무시): {e}")

        logger.info(f"[crawl] {schedule} complete: {configs_run} configs, {total_items} items")
        return {
            "schedule": schedule,
            "configs_run": configs_run,
            "total_items": total_items,
            "results": results_summary,
        }

    finally:
        db.close()


@celery.task(name="app.tasks.crawl_tasks.crawl_agency")
def crawl_agency(agency_code: str) -> dict:
    """특정 기관의 활성 크롤링 설정을 모두 실행합니다.

    API 수동 트리거용.
    """
    logger.info(f"[crawl] Manual crawl for {agency_code}")
    db = _get_sync_session()

    try:
        agency = db.query(Agency).filter(Agency.code == agency_code.upper()).first()
        if not agency:
            return {"error": f"Agency '{agency_code}' not found"}

        configs = (
            db.query(CrawlConfig)
            .filter(CrawlConfig.agency_id == agency.id, CrawlConfig.is_active == True)
            .all()
        )

        results_summary: list[dict] = []

        for config in configs:
            result: CrawlResult = _run_async(_run_crawl_config(config, agency.code))

            run = CrawlRun(
                agency_id=agency.id,
                config_id=config.id,
                status=CrawlRunStatus.SUCCESS if result.success else CrawlRunStatus.FAILED,
                started_at=result.started_at,
                finished_at=result.finished_at or datetime.now(),
                items_found=result.count,
                items_new=result.count,
                error_message=result.error,
            )
            db.add(run)
            db.commit()

            results_summary.append({
                "config": config.label,
                "success": result.success,
                "items": result.count,
                "error": result.error,
            })

        return {"agency": agency_code, "results": results_summary}

    finally:
        db.close()


@celery.task(name="app.tasks.crawl_tasks.check_law_amendments")
def check_law_amendments() -> dict:
    """법령 트래커의 개정 감지 cron을 원격 트리거합니다.

    Vercel cron이 Hobby 플랜 제한으로 동작하지 않아,
    Mac mini의 Celery Beat에서 HTTP 호출로 대신 실행합니다.

    필요 env:
      - LAW_TRACKER_URL (default: https://itpe-law-tracker.vercel.app)
      - LAW_TRACKER_CRON_SECRET (Vercel CRON_SECRET과 동일값)
    """
    import httpx
    from app.config import get_settings

    settings = get_settings()
    base = settings.law_tracker_url
    secret = settings.law_tracker_cron_secret

    if not secret:
        logger.error("[law-amend] LAW_TRACKER_CRON_SECRET 미설정")
        return {"error": "LAW_TRACKER_CRON_SECRET not configured"}

    results: dict = {"endpoints": {}}
    for endpoint in ("/api/cron/check-amendments", "/api/cron/generate-summaries"):
        url = f"{base}{endpoint}"
        try:
            with httpx.Client(timeout=300) as client:
                res = client.get(url, headers={"Authorization": f"Bearer {secret}"})
            data = res.json() if res.is_success else {}
            results["endpoints"][endpoint] = {
                "status": res.status_code,
                "new_amendments": data.get("newAmendments"),
                "errors": data.get("errors"),
            }
            logger.info(f"[law-amend] {endpoint} → {res.status_code}")
        except Exception as e:
            logger.error(f"[law-amend] {endpoint} 실패: {e}")
            results["endpoints"][endpoint] = {"error": str(e)}

    return results


@celery.task(name="app.tasks.crawl_tasks.check_legal_basis_updates")
def check_legal_basis_updates() -> dict:
    """법제처 행정규칙 API로 고시/훈령 변경을 감지합니다.

    kordoc MCP의 search_admin_rule 또는 법제처 DRF API를 사용하여
    추적 대상 고시/훈령의 최신 공포일을 조회하고,
    DB에 저장된 날짜와 비교합니다.

    TODO: 법제처 행정규칙 API 연동 구현
    - kordoc MCP discover_tools → execute_tool(search_admin_rule)
    - 또는 법제처 DRF admRulSearch.do API 직접 호출
    """
    logger.info("[legal-basis] Checking for 고시/훈령 updates")
    db = _get_sync_session()

    try:
        from app.models.guideline import LegalBasis

        bases = db.query(LegalBasis).filter(LegalBasis.law_api_id.isnot(None)).all()

        if not bases:
            logger.info("[legal-basis] No legal bases with law_api_id to check")
            return {"checked": 0, "updates_found": 0}

        checked = 0
        updates_found = 0

        for basis in bases:
            # TODO: 법제처 API로 최신 공포일 조회
            # latest = await fetch_admin_rule_info(basis.law_api_id)
            # if latest.promulgation_date > basis.promulgation_date:
            #     updates_found += 1
            #     # DB 업데이트 + 갭 분석 트리거
            checked += 1

        logger.info(f"[legal-basis] Checked {checked}, updates: {updates_found}")
        return {"checked": checked, "updates_found": updates_found}

    finally:
        db.close()
