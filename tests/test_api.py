"""Tests for allratestoday.api against responses recorded from the live API
on 2026-11-01 (tests/fixtures). live_rates_documented_shape.json is built from
the shape in https://allratestoday.com/docs/ because a keyed call was not made."""

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from allratestoday import api  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def load(name):
	return json.loads((FIX / name).read_text())


def rate_map(result):
	return {(r.from_currency, r.to_currency, r.applies_to): r.rate for r in result.rates}


class FakeResponse(io.BytesIO):
	def __enter__(self):
		return self

	def __exit__(self, *a):
		return False


def opener_for(routes, seen=None):
	"""routes: {url_prefix: fixture_name or (status, fixture_name)}"""

	def _open(req, timeout=None):
		if seen is not None:
			seen.append(req)
		for prefix, target in routes.items():
			if req.full_url.startswith(prefix):
				if isinstance(target, tuple):
					status, name = target
					raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO((FIX / name).read_bytes()))
				return FakeResponse((FIX / target).read_bytes())
		raise AssertionError(f"unexpected URL {req.full_url}")

	return _open


# ----------------------------------------------------------------- central bank


def test_rbi_direction_and_date():
	res = api.parse_central_bank(load("rbi_latest.json"), "INR")
	m = rate_map(res)
	assert res.rate_date == "2026-10-30"
	assert m[("USD", "INR", api.BOTH)] == 95.2492  # 1 USD = 95.2492 INR
	assert m[("EUR", "INR", api.BOTH)] == 106.5451
	assert not res.missing
	assert all(not r.derived for r in res.rates)


def test_rbi_per_100_units_already_normalised():
	m = rate_map(api.parse_central_bank(load("rbi_latest.json"), "INR", ["JPY", "IDR"]))
	# RBI publishes JPY and IDR per 100 units; the API returns per-unit values.
	assert m[("JPY", "INR", api.BOTH)] == pytest.approx(0.6045)
	assert m[("IDR", "INR", api.BOTH)] == pytest.approx(0.00524427)


def test_rbi_missing_currency_reported():
	res = api.parse_central_bank(load("rbi_latest.json"), "INR", "usd, chf")
	assert [r.from_currency for r in res.rates] == ["USD"]
	assert res.missing == ["CHF"]


def test_cbic_import_for_both_without_split():
	res = api.parse_central_bank(load("cbic_latest.json"), "INR", ["USD", "EUR"])
	m = rate_map(res)
	assert res.rate_date == "2026-10-16"
	raw = {(r["base"], r["type"]): r["value"] for r in load("cbic_latest.json")["rates"]}
	assert m[("USD", "INR", api.BOTH)] == raw[("USD", "import")]
	assert len(res.rates) == 2


def test_cbic_split_import_buying_export_selling():
	res = api.parse_central_bank(load("cbic_latest.json"), "INR", ["USD"], split=True)
	m = rate_map(res)
	raw = {(r["base"], r["type"]): r["value"] for r in load("cbic_latest.json")["rates"]}
	assert m[("USD", "INR", api.BUYING)] == raw[("USD", "import")]
	assert m[("USD", "INR", api.SELLING)] == raw[("USD", "export")]
	assert m[("USD", "INR", api.BUYING)] > m[("USD", "INR", api.SELLING)]


def test_cbic_all_currencies_when_blank():
	res = api.parse_central_bank(load("cbic_latest.json"), "INR")
	assert len(res.rates) == 22
	assert "INR" not in {r.from_currency for r in res.rates}


def test_ecb_inr_base_cross_rates():
	res = api.parse_central_bank(load("ecb_latest.json"), "INR", ["EUR", "USD", "GBP"])
	m = rate_map(res)
	raw = {r["quote"]: r["value"] for r in load("ecb_latest.json")["rates"]}
	assert m[("EUR", "INR", api.BOTH)] == raw["INR"]
	assert m[("USD", "INR", api.BOTH)] == pytest.approx(raw["INR"] / raw["USD"], rel=1e-9)
	derived = {r.from_currency: r.derived for r in res.rates}
	assert derived == {"EUR": False, "USD": True, "GBP": True}


def test_ecb_eur_base_inverts():
	m = rate_map(api.parse_central_bank(load("ecb_latest.json"), "EUR", ["USD"]))
	raw = {r["quote"]: r["value"] for r in load("ecb_latest.json")["rates"]}
	assert m[("USD", "EUR", api.BOTH)] == pytest.approx(1 / raw["USD"], rel=1e-8)


def test_central_bank_bad_shape():
	with pytest.raises(api.ApiError):
		api.parse_central_bank(load("central_bank_not_found.json"), "INR")


# ------------------------------------------------------------------- bank cards


def test_sbi_card_tt_buying_for_both():
	res = api.parse_bank_card(load("bank_sbi_all.json"), "INR", ["USD", "EUR", "JPY"])
	m = rate_map(res)
	rows = {r["ccy"]: r for r in load("bank_sbi_all.json")["rates"]}
	assert res.rate_date == "2026-11-01"
	assert m[("EUR", "INR", api.BOTH)] == rows["EUR"]["buy"]
	assert m[("JPY", "INR", api.BOTH)] == rows["JPY"]["buy"]  # per unit


def test_sbi_card_split():
	res = api.parse_bank_card(load("bank_sbi_usd.json"), "INR", ["USD"], split=True)
	m = rate_map(res)
	assert m[("USD", "INR", api.BUYING)] == 95.21  # bank sells to you
	assert m[("USD", "INR", api.SELLING)] == 94.36  # bank buys from you


def test_bank_card_single_currency_shape_and_missing():
	res = api.parse_bank_card(load("bank_axis_usd.json"), "INR", ["USD", "EUR"])
	assert [r.from_currency for r in res.rates] == ["USD"]
	assert res.missing == ["EUR"]


def test_bank_card_wrong_base():
	with pytest.raises(api.ApiError):
		api.parse_bank_card(load("bank_sbi_all.json"), "USD")


# ------------------------------------------------------------------------- live


def test_live_inverts_to_erpnext_direction():
	res = api.parse_live(load("live_rates_documented_shape.json"), "INR", ["USD", "EUR", "GBP"])
	m = rate_map(res)
	assert m[("USD", "INR", api.BOTH)] == pytest.approx(1 / 0.0104986, rel=1e-9)
	assert res.missing == ["GBP"]
	assert res.rate_date == "2026-11-01"


def test_live_error_payload():
	with pytest.raises(api.ApiError, match="Authentication required"):
		api.parse_live(load("live_unauthorized.json"), "INR")


# ------------------------------------------------------------------ fetch_rates


def test_fetch_rbi_keyless_no_headers_sent():
	seen = []
	res = api.fetch_rates(api.SOURCE_RBI, "INR", ["USD"], opener=opener_for({api.BASE_URL + "/api/open/central-bank/rbi": "rbi_latest.json"}, seen))
	assert res.rates[0].rate == 95.2492 and res.source_label == "rbi"
	assert "Authorization" not in seen[0].headers and "?" not in seen[0].full_url


def test_fetch_other_code():
	res = api.fetch_rates(api.SOURCE_OTHER, "INR", ["EUR"], other_code=" ECB ", opener=opener_for({api.BASE_URL + "/api/open/central-bank/ecb": "ecb_latest.json"}))
	assert res.rates[0].rate == 106.084


def test_fetch_bank_uses_all_currency_call():
	seen = []
	api.fetch_rates(api.SOURCE_BANK, "INR", ["USD", "EUR"], bank="sbi", opener=opener_for({api.BASE_URL + "/api/open/bank-rates/in/sbi": "bank_sbi_all.json"}, seen))
	assert len(seen) == 1 and seen[0].full_url.endswith("/in/sbi")


def test_fetch_unknown_bank():
	with pytest.raises(api.ApiError):
		api.fetch_rates(api.SOURCE_BANK, bank="hdfc", opener=opener_for({}))


def test_fetch_live_sends_bearer_and_batches_targets():
	seen = []
	res = api.fetch_rates(api.SOURCE_LIVE, "INR", ["USD", "EUR", "JPY"], api_key="k123", opener=opener_for({api.BASE_URL + "/api/v1/rates": "live_rates_documented_shape.json"}, seen))
	assert seen[0].get_header("Authorization") == "Bearer k123"
	assert seen[0].full_url == api.BASE_URL + "/api/v1/rates?source=INR&target=EUR,JPY,USD"
	assert "k123" not in seen[0].full_url
	assert len(res.rates) == 3


def test_fetch_live_requires_key():
	with pytest.raises(api.ApiError, match="API key"):
		api.fetch_rates(api.SOURCE_LIVE, "INR", ["USD"], opener=opener_for({}))


def test_http_error_message_surfaces():
	op = opener_for({api.BASE_URL + "/api/open/central-bank/fbil": (404, "central_bank_not_found.json")})
	with pytest.raises(api.ApiError, match="HTTP 404.*No published table"):
		api.fetch_rates(api.SOURCE_OTHER, other_code="fbil", opener=op)


def test_invalid_inputs():
	with pytest.raises(api.ApiError):
		api.normalise_code("../x")
	with pytest.raises(api.ApiError):
		api.normalise_currencies("US")
	assert api.normalise_currencies("usd; eur  gbp,usd") == ["USD", "EUR", "GBP"]
	assert api.redact("https://x/y?source=USD&api_key=secret") == "https://x/y?source=USD&api_key=***"


# ---------------------------------------------------------------------- preset


def test_preset_keyless_matches_migrate_guide():
	p = api.erpnext_preset(api.SOURCE_RBI)
	assert p["service_provider"] == "Custom"
	assert p["api_endpoint"] == "https://allratestoday.com/api/compat/frankfurter/{transaction_date}"
	assert p["req_params"] == [{"key": "base", "value": "{from_currency}"}, {"key": "symbols", "value": "{to_currency}"}]
	assert p["result_key"] == [{"key": "rates"}, {"key": "{to_currency}"}]


def test_preset_result_key_resolves_on_recorded_response():
	# What ERPNext's validate_result does with the save-time USD->INR request.
	p = api.erpnext_preset(api.SOURCE_ECB)
	value = load("compat_frankfurter_usd_inr.json")
	for k in p["result_key"]:
		value = value[k["key"].format(transaction_date="2026-11-01", to_currency="INR", from_currency="USD")]
	assert isinstance(value, float) and value > 50


def test_preset_with_key_uses_bank_table():
	p = api.erpnext_preset(api.SOURCE_RBI, api_key="k")
	assert p["api_endpoint"] == "https://allratestoday.com/api/v1/central-bank/rbi/{transaction_date}"
	assert {"key": "api_key", "value": "k"} in p["req_params"]
	assert p["result_key"] == [{"key": "rate"}]
	assert api.erpnext_preset(api.SOURCE_OTHER, "k", "SNB")["api_endpoint"].endswith("/snb/{transaction_date}")
	# CBIC / bank cards cannot be served one-number-per-pair: keyless ECB preset
	assert api.erpnext_preset(api.SOURCE_CBIC, "k")["api_endpoint"] == api.ECB_PRESET_ENDPOINT
