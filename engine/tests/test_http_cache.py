"""טסטים לעוזר ETag/304 (תשתית 0.3)."""
import json

from _http_cache import etag_json_response, make_etag


def test_make_etag_deterministic_and_quoted():
    e1 = make_etag(b'{"a":1}')
    e2 = make_etag(b'{"a":1}')
    assert e1 == e2
    assert e1.startswith('"') and e1.endswith('"')


def test_make_etag_differs_for_different_bytes():
    assert make_etag(b'{"a":1}') != make_etag(b'{"a":2}')


def test_response_200_with_etag_when_no_if_none_match():
    payload = {"balance": 100, "positions": []}
    resp = etag_json_response(payload, if_none_match=None)
    assert resp.status_code == 200
    assert "etag" in {k.lower() for k in resp.headers.keys()}
    assert json.loads(bytes(resp.body)) == payload


def test_response_304_when_if_none_match_matches():
    payload = {"balance": 100}
    first = etag_json_response(payload, if_none_match=None)
    etag = first.headers["etag"]
    second = etag_json_response(payload, if_none_match=etag)
    assert second.status_code == 304
    assert bytes(second.body) == b""
    assert second.headers["etag"] == etag


def test_response_200_when_if_none_match_stale():
    payload = {"balance": 200}
    resp = etag_json_response(payload, if_none_match='"deadbeef"')
    assert resp.status_code == 200


def test_etag_changes_when_payload_changes():
    a = etag_json_response({"x": 1}, if_none_match=None).headers["etag"]
    b = etag_json_response({"x": 2}, if_none_match=None).headers["etag"]
    assert a != b


def test_key_order_does_not_change_etag():
    """סדר מפתחות שונה עם אותו תוכן -> אותו ETag (sort_keys)."""
    a = etag_json_response({"a": 1, "b": 2}, if_none_match=None).headers["etag"]
    b = etag_json_response({"b": 2, "a": 1}, if_none_match=None).headers["etag"]
    assert a == b
