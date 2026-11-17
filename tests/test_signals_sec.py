"""Offline checks for trade aggregation, sale scoring, and filing XML safety."""

import math

import pytest
from defusedxml.common import EntitiesForbidden

from fin_sets import signals_sec


@pytest.fixture
def document():
    return {
        "issuer": {"cik": "0000000001", "name": "Synthetic issuer", "ticker": None},
        "owner": {"name": "Synthetic owner", "roles": ["CFO"], "is_ceo_cfo": True},
        "rows": [], "planned": False,
    }


def trade(code="P", shares=600, price=10, after=6000, planned=False, day="2026-01-02"):
    return {"date": day, "code": code, "shares": shares, "price": price,
            "after": after, "planned": planned, "ad": "A" if code == "P" else "D"}


def signals(document):
    return signals_sec.form4_signals(document, "0000000001-26-000001", "2026-01-05",
                                     "0000000001", {"0000000001": "TEST"})


def test_buys_aggregate_same_day_before_value_threshold(document):
    document["rows"] = [trade(), trade(shares=400, price=20, after=10_000),
                        trade(shares=1000, after=5000, day="2026-01-03")]

    first, second = signals(document)

    assert first["ticker"] == "TEST"
    assert first["value"] == 14_000
    assert first["detail"]["shares"] == 1000
    assert first["detail"]["vwap"] == 14
    assert first["detail"]["post_trade_shares"] == 10_000
    assert first["detail"]["pct_of_holdings"] == 10
    assert first["strength"] == pytest.approx(30 + 12 * math.log10(1.4) + 15 + 10)
    assert second["value"] == 10_000
    assert second["detail"]["trade_date"] == "2026-01-03"
    assert first["dedupe_key"] != second["dedupe_key"]


@pytest.mark.parametrize("code, value, expected_kind", [
    ("P", 9999, None), ("P", 10_000, "insider_buy"),
    ("S", 249_999, None), ("S", 250_000, "insider_sell"),
])
def test_trade_value_threshold_is_inclusive(document, code, value, expected_kind):
    document["rows"] = [trade(code=code, shares=value, price=1, after=1_000_000)]

    result = signals(document)

    assert [s["kind"] for s in result] == ([] if expected_kind is None else [expected_kind])


@pytest.mark.parametrize("planned, expected_strength", [(False, 25), (True, 7.5)])
def test_sale_fraction_uses_pre_sale_holdings(document, planned, expected_strength):
    document["rows"] = [trade(code="S", shares=2500, price=100, after=7500, planned=planned)]

    signal, = signals(document)

    assert signal["direction"] == "bearish"
    assert signal["detail"]["pct_of_holdings"] == 25
    assert signal["detail"]["planned_10b5_1"] is planned
    assert signal["strength"] == expected_strength


@pytest.mark.parametrize("parser, filing", [
    (signals_sec.parse_form4_doc,
     '<!DOCTYPE ownershipDocument [<!ENTITY test "unexpected">]>'
     '<ownershipDocument>&test;</ownershipDocument>'),
    (signals_sec.parse_form144,
     '<XML><?xml version="1.0"?><!DOCTYPE edgarSubmission [<!ENTITY test "unexpected">]>'
     '<edgarSubmission>&test;</edgarSubmission></XML>'),
])
def test_filing_parsers_reject_xml_entities(parser, filing):
    with pytest.raises(EntitiesForbidden):
        parser(filing)
