"""금융위 보도자료 중복 버전 회귀 테스트.

버그: 같은 금융위 보도자료에 매일 새 버전이 붙었다(11/17~11/21 하루 1개씩).
  1) 게시물이 목록 2페이지로 밀리면 링크의 curPage 만 바뀌어 URL 일치 검사를 비껴감
  2) 게시물 번호가 경로(/no010101/88133)에 있어 콘텐츠 지문이 None
  3) ul/li 목록이라 날짜를 못 읽어 published_date = 크롤한 날 → 날짜 fallback도 통과
"""

from datetime import date

from app.crawlers.bbs import BbsCrawler
from app.services.guideline_sync import _url_key, content_fingerprint
from app.services.url_key import normalize_decision_url

Q = "?srchCtgry=&curPage={page}&srchKey=&srchText=&srchBeginDt=&srchEndDt="
FSC_P1 = "https://www.fsc.go.kr/no010101/88133" + Q.format(page=1)
FSC_P2 = "https://www.fsc.go.kr/no010101/88133" + Q.format(page=2)

# 실제 금융위 목록 마크업(2026-11-21)을 줄인 것
FSC_LIST_HTML = """
<div class="board-list-wrap"><ul>
  <li><div class="inner">
    <div class="count">15475</div>
    <div class="cont">
      <div class="subject">
        <a href="/no010101/88162?srchCtgry=&curPage=1&srchKey=&srchText=&srchBeginDt=&srchEndDt="
           title="t">全금융권이 IT자산 식별·관리를 의무화하겠습니다.</a>
      </div>
      <div class="info"><span>담당부서 : 금융안전과</span><span>조회수 : 16419</span></div>
      <div class="file"><div class="file-list">
        <a href="/comm/getFile?srvcId=BBSTY1&amp;upperNo=88162&amp;fileTy=ATTACH&amp;fileNo=2">
          <span class="name">261117(보도자료) ….pdf</span></a>
      </div></div>
    </div>
    <div class="day">2026-11-18</div>
  </div></li>
</ul></div>
"""


def test_page_shift_keeps_same_url_key():
    assert _url_key(FSC_P1) == _url_key(FSC_P2)
    assert _url_key(FSC_P1) == "https://www.fsc.go.kr/no010101/88133"


def test_decision_cache_key_ignores_list_context():
    assert normalize_decision_url(FSC_P1) == normalize_decision_url(FSC_P2)


def test_distinct_fsc_posts_stay_distinct():
    other = FSC_P1.replace("88133", "88134")
    assert _url_key(FSC_P1) != _url_key(other)
    assert content_fingerprint(None, FSC_P1) != content_fingerprint(None, other)


def test_fsc_path_post_id_fingerprint():
    fp1 = content_fingerprint(None, FSC_P1)
    assert fp1 == "path:no010101/88133"
    assert content_fingerprint(None, FSC_P2) == fp1
    # 다른 게시판(고시·훈령)의 같은 번호와는 구분
    assert content_fingerprint(None, "https://www.fsc.go.kr/po040200/88133") != fp1


def test_non_fsc_fingerprint_unchanged():
    assert content_fingerprint(None, "https://www.kisa.or.kr/401/form?postSeq=3736") is None
    assert content_fingerprint(None, "https://x.go.kr/view?nttNo=42") == "seq:42"


def test_fsc_list_date_parsed_with_day_selector():
    crawler = BbsCrawler(
        "FSC",
        "https://www.fsc.go.kr/no010101",
        list_selector=".board-list-wrap > ul > li",
        title_selector=".subject a",
        date_selector=".day",
        pagination_param="curPage",
    )
    items = crawler._extract_items_from_html(
        FSC_LIST_HTML, "https://www.fsc.go.kr/no010101?curPage=1"
    )
    assert len(items) == 1
    assert items[0].published_date == date(2026, 11, 18)
    assert items[0].url.startswith("https://www.fsc.go.kr/no010101/88162")


def test_fsc_registry_targets_have_date_selector():
    from app.crawlers.registry import AGENCY_SEEDS

    fsc = next(s for s in AGENCY_SEEDS if s.code == "FSC")
    bbs = [t for t in fsc.targets if t.source_type == "bbs_list"]
    assert bbs and all(t.date_selector == ".day" for t in bbs)
