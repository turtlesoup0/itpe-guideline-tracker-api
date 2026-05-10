"""
크롤러 라우팅 공통 디스패처.

API 라우트(`POST /crawl/{agency}`)와 Celery 태스크(`crawl_by_schedule`)가
동일한 라우팅 로직을 공유하도록 추출. 새 크롤러 추가 시 여기 한 곳에만
분기 추가하면 양쪽에서 자동으로 동작.

라우팅 우선순위 (URL 기반 → source_type 기반):
1. static_pubs 프로필 매칭 (URL 완전 일치)
2. bbs_detail_scan 프로필 매칭 (URL 완전 일치)
3. source_type: RSS → RssCrawler
4. source_type: BBS_LIST → BbsCrawler
5. source_type: LAW_API → law_api.crawl_admin_rules
"""

from datetime import datetime

from app.crawlers.base import CrawlResult
from app.crawlers.bbs import BbsCrawler
from app.crawlers.rss import RssCrawler
from app.models.agency import CrawlConfig, CrawlSourceType


async def run_crawl_config(config: CrawlConfig, agency_code: str) -> CrawlResult:
    """CrawlConfig를 적절한 크롤러로 라우팅하여 실행."""
    keyword_list = (
        config.keyword_filter.split(",") if config.keyword_filter else []
    )

    # 1. Static Publications Page (URL 정확 매칭)
    from app.crawlers.static_pubs import (
        crawl_static_pubs,
        get_profiles as get_static_pubs_profiles,
    )
    for pub_profile in get_static_pubs_profiles(agency_code):
        if config.url == pub_profile.url:
            return await crawl_static_pubs(pub_profile, keyword_filter=keyword_list)

    # 2. BBS Detail Scan (list JS + detail SSR, URL 매칭)
    from app.crawlers.bbs_detail_scan import (
        crawl_bbs_detail_scan,
        get_profile_by_url,
    )
    profile = get_profile_by_url(agency_code, config.url)
    if profile is not None:
        return await crawl_bbs_detail_scan(
            profile=profile,
            keyword_filter=keyword_list,
            config_label=config.label,
        )

    # 3. RSS
    if config.source_type == CrawlSourceType.RSS:
        crawler = RssCrawler(
            agency_code=agency_code,
            feed_url=config.url,
            keyword_filter=keyword_list,
            config_label=config.label,
        )
        async with crawler:
            return await crawler.crawl()

    # 4. BBS list
    if config.source_type == CrawlSourceType.BBS_LIST:
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
        async with crawler:
            return await crawler.crawl()

    # 5. 법제처 API
    if config.source_type == CrawlSourceType.LAW_API:
        from app.crawlers.law_api import crawl_admin_rules
        return await crawl_admin_rules(agency_code)

    # Unknown
    return CrawlResult(
        agency_code=agency_code,
        config_label=config.label,
        started_at=datetime.now(),
        finished_at=datetime.now(),
        error=f"Unsupported source type: {config.source_type}",
    )
