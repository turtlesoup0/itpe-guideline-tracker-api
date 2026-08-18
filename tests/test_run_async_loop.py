"""_run_async 이벤트 루프 재사용 버그 재현 테스트.

버그: Celery 태스크의 _run_async가 호출마다 새 이벤트 루프를 만드는데,
크롤러(bbs_detail_scan 등)가 사용하는 전역 async 엔진(app.db.session.engine)의
커넥션 풀에는 이전 루프에 바인딩된 커넥션이 남는다. 두 번째 호출부터
"got Future attached to a different loop"로 실패 (FSI 자료마당 5연속 FAILED 원인).

재현 조건: 동일 프로세스에서 _run_async를 2회 호출, 각 호출이 전역 엔진으로 쿼리.
"""

import pytest
from sqlalchemy import text

from app.db.session import async_session_factory
from app.tasks.crawl_tasks import _run_async


async def _touch_db_via_global_engine() -> int:
    """bbs_detail_scan._get_last_id와 동일하게 전역 엔진 세션으로 쿼리."""
    async with async_session_factory() as session:
        result = await session.execute(text("SELECT 1"))
        return result.scalar_one()


def test_run_async_twice_with_global_async_engine():
    """_run_async 연속 2회 호출이 모두 성공해야 한다.

    수정 전: 두 번째 호출에서 asyncpg Future가 첫 번째(이미 닫힌) 루프에
    붙어 있어 RuntimeError/InterfaceError 발생.
    """
    assert _run_async(_touch_db_via_global_engine()) == 1
    # 버그 시 여기서 "attached to a different loop" 계열 예외
    assert _run_async(_touch_db_via_global_engine()) == 1
    # 3회째까지 안정 동작 확인
    assert _run_async(_touch_db_via_global_engine()) == 1
