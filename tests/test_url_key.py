"""판정 캐시 키 정규화 테스트.

버그: 행안부 게시판 URL에 매 요청 달라지는 `;jsessionid=…` 가 들어 있어,
     같은 게시물이 크롤마다 다른 키로 보이며 판정 캐시(수동 제외 포함)를
     통째로 비껴갔다 — 제외한 항목이 새 항목으로 되살아난다.
"""

from app.services.url_key import normalize_decision_url

MOIS = (
    "https://www.mois.go.kr/frt/bbs/type001/commonSelectBoardArticle.do"
    ";jsessionid={session}?bbsId=BBSMSTR_000000000016&nttId=128632"
)


def test_session_token_does_not_change_key():
    a = normalize_decision_url(MOIS.format(session="xgdR-lxXxhaZ.node20"))
    b = normalize_decision_url(MOIS.format(session="ZZZZotherSession.node40"))
    assert a == b
    assert "jsessionid" not in a
    # 게시물 식별자는 살아 있어야 한다
    assert "nttId=128632" in a


def test_distinct_posts_keep_distinct_keys():
    a = normalize_decision_url(MOIS.format(session="s1").replace("128632", "128632"))
    b = normalize_decision_url(MOIS.format(session="s1").replace("128632", "999999"))
    assert a != b


def test_volatile_query_params_dropped_but_others_kept():
    key = normalize_decision_url("https://x.test/a?postSeq=10&_=1699999999&page=2")
    assert key == "https://x.test/a?postSeq=10&page=2"


def test_plain_url_unchanged_and_empty_passthrough():
    url = "https://www.kisa.or.kr/401/form?postSeq=3736&page=1"
    assert normalize_decision_url(url) == url
    assert normalize_decision_url(None) is None
    assert normalize_decision_url("") == ""
