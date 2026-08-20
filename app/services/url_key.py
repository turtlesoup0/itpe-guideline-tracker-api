"""판정 캐시 키용 URL 정규화.

crawl_decisions.url 은 "이 게시물을 이미 판정했는가"의 키다. 그런데 일부
기관 게시판은 매 요청마다 바뀌는 세션 토큰을 URL에 심는다:

    …/commonSelectBoardArticle.do;jsessionid=xgdR-lxX…?bbsId=…&nttId=128632
                                  ^^^^^^^^^^^^^^^^^^^ 크롤할 때마다 다름

이 상태로는 같은 게시물이 매번 새 URL로 보여 판정 캐시(제외 포함)를 통째로
비껴간다 — 제외 처리한 항목이 다음 크롤에 새 항목으로 되살아난다.

그래서 키를 만들 때 세션성 요소만 벗겨낸다. 벗겨낼 대상은 화이트리스트로
한정한다: 모르는 파라미터를 임의로 지우면 서로 다른 게시물이 같은 키로
합쳐질 수 있고, 그건 캐시가 새는 것보다 나쁘다.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 경로에 붙는 세션 파라미터 (;jsessionid=... 형태)
_PATH_SESSION_RE = re.compile(r";(?:jsessionid|phpsessid|sid)=[^/;?#]*", re.I)

# 쿼리스트링의 세션·캐시버스터 파라미터
_VOLATILE_QUERY_KEYS = {
    "jsessionid",
    "phpsessid",
    "sessionid",
    "session_id",
    "aspxautodetectcookiesupport",
    "_",
    "timestamp",
    "cachebuster",
}


def normalize_decision_url(url: str | None) -> str | None:
    """판정 캐시 키로 쓸 URL을 만든다.

    - `;jsessionid=…` 등 경로 세션 파라미터 제거
    - 세션·캐시버스터 쿼리 파라미터 제거 (그 외 파라미터는 순서까지 보존)
    - scheme/host 소문자화, 빈 쿼리·프래그먼트 정리

    입력이 비었거나 파싱할 수 없으면 원본을 그대로 돌려준다 — 키를 만들지
    못하는 것보다 원본 키가 낫다.
    """
    if not url:
        return url

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url

    path = _PATH_SESSION_RE.sub("", parts.path)

    query = parts.query
    if query:
        kept = [
            (k, v)
            for k, v in parse_qsl(query, keep_blank_values=True)
            if k.lower() not in _VOLATILE_QUERY_KEYS
        ]
        query = urlencode(kept)

    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, query, "")
    )
