"""
Shared manifest 생성 서비스.

guideline-manifest.json을 itpe-shared-manifests 디렉토리에 씁니다.
이 매니페스트는 법령 트래커가 읽기 전용으로 참조합니다.

갱신 시점:
- POST /crawl/{agency_code} 완료 후
- Celery Beat 크롤 완료 후
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# shared-manifests 디렉토리 (프로젝트 루트의 형제)
MANIFEST_DIR = Path(__file__).resolve().parent.parent.parent.parent / "itpe-shared-manifests"
MANIFEST_PATH = MANIFEST_DIR / "guideline-manifest.json"

SCHEMA_VERSION = "1.0.0"
TTL_HOURS = 48  # 크롤링 최소 주기(일간)보다 여유 있게


def regenerate_manifest_sync(db_session) -> dict:
    """동기 DB 세션으로 guideline-manifest.json을 재생성합니다.

    Celery 태스크(동기)에서 호출됩니다.

    Returns:
        {"legal_bases": N, "guidelines": N, "path": str}
    """
    from app.models.agency import Agency
    from app.models.guideline import Guideline, LegalBasis

    # ── 기관 코드 맵 ──
    agencies = db_session.query(Agency).all()
    agency_map = {a.id: {"code": a.code, "name": a.short_name} for a in agencies}

    # ── 법적 근거 ──
    bases = db_session.query(LegalBasis).all()
    legal_bases_out = []
    for b in bases:
        ag = agency_map.get(b.agency_id, {"code": "?", "name": "?"})
        legal_bases_out.append({
            "id": b.id,
            "title": b.title,
            "basis_type": b.basis_type.value if hasattr(b.basis_type, "value") else str(b.basis_type),
            "agency_code": ag["code"],
            "agency_name": ag["name"],
            "parent_law_name": b.parent_law_name,
            "url": f"/legal-bases?agency_code={ag['code']}",
        })

    # ── 가이드라인 ──
    guidelines = db_session.query(Guideline).all()
    guidelines_out = []
    for g in guidelines:
        ag = agency_map.get(g.agency_id, {"code": "?", "name": "?"})
        guidelines_out.append({
            "id": g.id,
            "title": g.title,
            "category": g.category.value if hasattr(g.category, "value") else str(g.category),
            "agency_code": ag["code"],
            "agency_name": ag["name"],
            "latest_published_date": None,  # 버전 조회 없이 간단히
            "url": f"/guidelines?agency_code={ag['code']}",
        })

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ttl_hours": TTL_HOURS,
        "legal_bases": legal_bases_out,
        "guidelines": guidelines_out,
    }

    return _write_manifest(manifest)


async def regenerate_manifest_async(db_session) -> dict:
    """비동기 DB 세션으로 guideline-manifest.json을 재생성합니다.

    FastAPI 엔드포인트(비동기)에서 호출됩니다.

    Returns:
        {"legal_bases": N, "guidelines": N, "path": str}
    """
    from sqlalchemy import select
    from app.models.agency import Agency
    from app.models.guideline import Guideline, LegalBasis

    # ── 기관 코드 맵 ──
    result = await db_session.execute(select(Agency))
    agencies = result.scalars().all()
    agency_map = {a.id: {"code": a.code, "name": a.short_name} for a in agencies}

    # ── 법적 근거 ──
    result = await db_session.execute(select(LegalBasis))
    bases = result.scalars().all()
    legal_bases_out = []
    for b in bases:
        ag = agency_map.get(b.agency_id, {"code": "?", "name": "?"})
        legal_bases_out.append({
            "id": b.id,
            "title": b.title,
            "basis_type": b.basis_type.value if hasattr(b.basis_type, "value") else str(b.basis_type),
            "agency_code": ag["code"],
            "agency_name": ag["name"],
            "parent_law_name": b.parent_law_name,
            "url": f"/legal-bases?agency_code={ag['code']}",
        })

    # ── 가이드라인 ──
    result = await db_session.execute(select(Guideline))
    guidelines = result.scalars().all()
    guidelines_out = []
    for g in guidelines:
        ag = agency_map.get(g.agency_id, {"code": "?", "name": "?"})
        guidelines_out.append({
            "id": g.id,
            "title": g.title,
            "category": g.category.value if hasattr(g.category, "value") else str(g.category),
            "agency_code": ag["code"],
            "agency_name": ag["name"],
            "latest_published_date": None,
            "url": f"/guidelines?agency_code={ag['code']}",
        })

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ttl_hours": TTL_HOURS,
        "legal_bases": legal_bases_out,
        "guidelines": guidelines_out,
    }

    return _write_manifest(manifest)


def _write_manifest(manifest: dict) -> dict:
    """매니페스트를 파일에 씁니다."""
    try:
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        count = {
            "legal_bases": len(manifest["legal_bases"]),
            "guidelines": len(manifest["guidelines"]),
            "path": str(MANIFEST_PATH),
        }
        logger.info(
            "[manifest] guideline-manifest.json 갱신: "
            "법적근거 %d건, 가이드라인 %d건",
            count["legal_bases"],
            count["guidelines"],
        )
        return count
    except Exception as e:
        logger.error("[manifest] guideline-manifest.json 쓰기 실패: %s", e)
        return {"error": str(e)}
