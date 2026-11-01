"""Fetch and parse AllRatesToday exchange rates.

Pure Python, standard library only, no ``frappe`` import, so it can be unit
tested without a bench. ``allratestoday.sync`` turns the ``Rate`` rows this
module returns into ERPNext ``Currency Exchange`` records.

Direction convention (the same as ERPNext): a ``Rate`` means
"1 ``from_currency`` = ``rate`` ``to_currency``". RBI publishes USD/INR as
1 USD = X INR, so the row is from_currency=USD, to_currency=INR, rate=X.
The API already normalises per-100 quotes (JPY, IDR, KRW) to per-unit values,
so no unit scaling is applied here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

BASE_URL = "https://allratestoday.com"
USER_AGENT = "allratestoday-frappe/0.1.0 (+https://github.com/AllRates-Today/allratestoday-frappe)"
TIMEOUT = 30
PRECISION = 9  # ERPNext stores exchange_rate with float precision 9

# Settings "source" values. Kept here so the DocType, sync and tests agree.
SOURCE_RBI = "RBI/FBIL reference (keyless)"
SOURCE_CBIC = "CBIC customs (keyless)"
SOURCE_ECB = "ECB reference (keyless)"
SOURCE_BANK = "Indian bank TT card (keyless)"
SOURCE_LIVE = "Live market rates (API key)"
SOURCE_OTHER = "Other central bank code"

SOURCES = (SOURCE_RBI, SOURCE_CBIC, SOURCE_ECB, SOURCE_BANK, SOURCE_LIVE, SOURCE_OTHER)
CENTRAL_BANK_CODES = {SOURCE_RBI: "rbi", SOURCE_CBIC: "cbic", SOURCE_ECB: "ecb"}
INDIAN_BANKS = ("sbi", "axis", "bob", "bom", "canara", "iob", "union")

# Which records a Rate should become. "both" = for_buying and for_selling.
BOTH, BUYING, SELLING = "both", "buying", "selling"

# Currency Exchange Settings presets (see https://allratestoday.com/migrate/erpnext/).
ECB_PRESET_ENDPOINT = BASE_URL + "/api/compat/frankfurter/{transaction_date}"
CB_PRESET_ENDPOINT = BASE_URL + "/api/v1/central-bank/{bank}/{transaction_date}"


class ApiError(Exception):
	"""A request failed or the response was not in the expected shape."""


@dataclass(frozen=True)
class Rate:
	date: str  # YYYY-MM-DD
	from_currency: str
	to_currency: str
	rate: float
	applies_to: str = BOTH  # BOTH, BUYING or SELLING
	derived: bool = False  # cross-computed or inverted, not published as such


@dataclass
class FetchResult:
	rates: list
	missing: list  # requested currencies the source did not cover
	rate_date: str | None = None
	source_label: str = ""


# --------------------------------------------------------------------------- HTTP


def get_json(url: str, headers: dict | None = None, timeout: int = TIMEOUT, opener=None):
	"""GET ``url`` and return decoded JSON. Raises ApiError with the API's own
	error message on HTTP errors. ``opener`` (a callable taking a Request and
	timeout) is injectable for tests."""
	req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
	open_fn = opener or urllib.request.urlopen
	try:
		with open_fn(req, timeout=timeout) as resp:
			body = resp.read()
	except urllib.error.HTTPError as e:
		raise ApiError(f"HTTP {e.code} from {redact(url)}: {_error_message(e.read())}") from None
	except (urllib.error.URLError, TimeoutError, OSError) as e:
		raise ApiError(f"Could not reach {redact(url)}: {e}") from None
	try:
		return json.loads(body)
	except ValueError:
		raise ApiError(f"Response from {redact(url)} is not JSON") from None


def _error_message(body: bytes) -> str:
	try:
		data = json.loads(body)
		if isinstance(data, dict) and data.get("error"):
			return str(data["error"])
	except ValueError:
		pass
	return body[:200].decode("utf-8", "replace")


def redact(url: str) -> str:
	"""Drop any api_key query parameter before a URL goes into a log."""
	parts = urllib.parse.urlsplit(url)
	q = [(k, "***" if k.lower() in ("api_key", "apikey") else v) for k, v in urllib.parse.parse_qsl(parts.query)]
	return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, safe="*,")))


def central_bank_url(code: str) -> str:
	return f"{BASE_URL}/api/open/central-bank/{urllib.parse.quote(normalise_code(code))}"


def bank_card_url(bank: str, country: str = "in") -> str:
	return f"{BASE_URL}/api/open/bank-rates/{country}/{urllib.parse.quote(normalise_code(bank))}"


def live_url(base: str, targets) -> str:
	params = {"source": base.upper()}
	if targets:
		params["target"] = ",".join(sorted({t.upper() for t in targets}))
	return f"{BASE_URL}/api/v1/rates?" + urllib.parse.urlencode(params, safe=",")


def normalise_code(code: str) -> str:
	code = (code or "").strip().lower()
	if not code or not all(c.isalnum() or c in "-_" for c in code):
		raise ApiError(f"Invalid institution code: {code!r}")
	return code


def normalise_currencies(values) -> list:
	"""Accept a list or a comma/space separated string of ISO codes."""
	if not values:
		return []
	if isinstance(values, str):
		values = values.replace(";", ",").replace(" ", ",").split(",")
	out = []
	for v in values:
		v = (v or "").strip().upper()
		if v and v not in out:
			if len(v) != 3 or not v.isalpha():
				raise ApiError(f"Not an ISO 4217 code: {v!r}")
			out.append(v)
	return out


# ------------------------------------------------------------------------ parsing


def _num(value):
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		return None
	return float(value) if value > 0 else None


def _round(x: float) -> float:
	return round(x, PRECISION)


def parse_central_bank(payload: dict, base: str, currencies=None, split: bool = False) -> FetchResult:
	"""Parse /api/open/central-bank/{code}.

	Shape: {"bank", "rate_date", "rates": [{"base","quote","type","value"}], ...}
	where each row reads "1 base = value quote".

	Produces "1 F = x base" for every requested currency F, using in order:
	a published F->base row, an inverted base->F row, or a cross through a
	pivot currency the table quotes both against (e.g. ECB's EUR).

	Tables with import/export rows (CBIC customs) become two records when
	``split`` is set: import -> for_buying (purchases), export -> for_selling
	(sales). Without ``split`` the import rate is written for both.
	"""
	if not isinstance(payload, dict) or not isinstance(payload.get("rates"), list):
		raise ApiError("Unexpected central-bank response: no rates array")
	base = base.upper()
	rate_date = str(payload.get("rate_date") or "")[:10]
	if not rate_date:
		raise ApiError("Unexpected central-bank response: no rate_date")

	# tables[type][(b, q)] = value
	tables: dict = {}
	for row in payload["rates"]:
		if not isinstance(row, dict):
			continue
		b, q, v = str(row.get("base", "")).upper(), str(row.get("quote", "")).upper(), _num(row.get("value"))
		if not (b and q and v) or b == q:
			continue
		tables.setdefault(str(row.get("type") or "reference"), {})[(b, q)] = v

	if not tables:
		raise ApiError("Central-bank response contains no usable rates")

	if "import" in tables or "export" in tables:
		if split:
			plan = [("import", BUYING), ("export", SELLING)]
		else:
			plan = [("import" if "import" in tables else "export", BOTH)]
	else:
		kind = "reference" if "reference" in tables else sorted(tables)[0]
		plan = [(kind, BOTH)]

	wanted = normalise_currencies(currencies)
	rates, missing = [], []
	for table_type, applies in plan:
		table = tables.get(table_type, {})
		codes = wanted or sorted({c for pair in table for c in pair} - {base})
		for f in codes:
			if f == base:
				continue
			value, derived = _resolve(table, f, base)
			if value is None:
				if f not in missing:
					missing.append(f)
				continue
			rates.append(Rate(rate_date, f, base, _round(value), applies, derived))
	return FetchResult(rates=rates, missing=missing, rate_date=rate_date)


def _resolve(table: dict, f: str, base: str):
	if (f, base) in table:
		return table[(f, base)], False
	if (base, f) in table:
		return 1.0 / table[(base, f)], True
	# Cross through a pivot P: value(F->base) = value(P->base) / value(P->F)
	for (p, q), p_base in table.items():
		if q == base and (p, f) in table:
			return p_base / table[(p, f)], True
	for (fx, p), f_p in table.items():  # F->P and P->base
		if fx == f and (p, base) in table:
			return f_p * table[(p, base)], True
	return None, False


def parse_bank_card(payload: dict, base: str = "INR", currencies=None, split: bool = False) -> FetchResult:
	"""Parse /api/open/bank-rates/in/{bank} (all currencies) or ?ccy=XXX (one).

	Rows are "home currency per 1 unit of foreign currency" with buy and sell.
	With ``split``: bank sell rate -> for_buying (you buy foreign currency to
	pay a supplier), bank buy rate -> for_selling (you sell the foreign
	currency a customer paid). Without ``split`` the TT buying rate is written
	for both, which is the rate Income-tax Rule 115 names.
	"""
	if not isinstance(payload, dict):
		raise ApiError("Unexpected bank-rates response")
	quote = str(payload.get("quote_ccy") or base).upper()
	if quote != base.upper():
		raise ApiError(f"Bank quotes in {quote}, but the base currency is {base.upper()}")
	rows = payload.get("rates")
	if rows is None and payload.get("ccy"):
		rows = [payload]
	if not isinstance(rows, list):
		raise ApiError("Unexpected bank-rates response: no rates")
	rate_date = str(payload.get("captured_at") or "")[:10]
	if not rate_date:
		raise ApiError("Unexpected bank-rates response: no captured_at")

	by_ccy = {}
	for row in rows:
		if isinstance(row, dict) and row.get("ccy"):
			by_ccy[str(row["ccy"]).upper()] = row

	wanted = normalise_currencies(currencies) or sorted(by_ccy)
	rates, missing = [], []
	for f in wanted:
		if f == quote:
			continue
		row = by_ccy.get(f) or {}
		buy, sell = _num(row.get("buy")), _num(row.get("sell"))
		if split:
			if not (buy and sell):
				missing.append(f)
				continue
			rates.append(Rate(rate_date, f, quote, _round(sell), BUYING))
			rates.append(Rate(rate_date, f, quote, _round(buy), SELLING))
		else:
			if not buy:
				missing.append(f)
				continue
			rates.append(Rate(rate_date, f, quote, _round(buy), BOTH))
	return FetchResult(rates=rates, missing=missing, rate_date=rate_date)


def parse_live(payload, base: str, currencies=None) -> FetchResult:
	"""Parse /api/v1/rates?source={base}&target=A,B (keyed).

	Shape: [{"rate", "source", "target", "time"}], each "1 source = rate target".
	One batched call returns base->F; ERPNext needs F->base, so each rate is
	inverted.
	"""
	if isinstance(payload, dict) and payload.get("error"):
		raise ApiError(str(payload["error"]))
	if not isinstance(payload, list):
		raise ApiError("Unexpected live-rates response: expected a list")
	base = base.upper()
	got = {}
	dates = []
	for row in payload:
		if not isinstance(row, dict):
			continue
		src, tgt, v = str(row.get("source", "")).upper(), str(row.get("target", "")).upper(), _num(row.get("rate"))
		if src != base or not tgt or not v or tgt == base:
			continue
		day = str(row.get("time") or "")[:10]
		got[tgt] = (1.0 / v, day)
		if day:
			dates.append(day)
	wanted = normalise_currencies(currencies) or sorted(got)
	rates, missing = [], []
	for f in wanted:
		if f == base:
			continue
		if f not in got or not got[f][1]:
			missing.append(f)
			continue
		value, day = got[f]
		rates.append(Rate(day, f, base, _round(value), BOTH, True))
	return FetchResult(rates=rates, missing=missing, rate_date=max(dates) if dates else None)


# --------------------------------------------------------------------- high level


def fetch_rates(
	source: str,
	base: str = "INR",
	currencies=None,
	split: bool = False,
	api_key: str | None = None,
	bank: str = "sbi",
	other_code: str | None = None,
	opener=None,
) -> FetchResult:
	"""Fetch and parse rates for one Settings configuration."""
	base = (base or "INR").upper()
	if source in CENTRAL_BANK_CODES or source == SOURCE_OTHER:
		code = CENTRAL_BANK_CODES.get(source) or normalise_code(other_code or "")
		result = parse_central_bank(get_json(central_bank_url(code), opener=opener), base, currencies, split)
		result.source_label = code
	elif source == SOURCE_BANK:
		if normalise_code(bank) not in INDIAN_BANKS:
			raise ApiError(f"Unknown bank {bank!r}; expected one of {', '.join(INDIAN_BANKS)}")
		result = parse_bank_card(get_json(bank_card_url(bank), opener=opener), base, currencies, split)
		result.source_label = normalise_code(bank)
	elif source == SOURCE_LIVE:
		if not api_key:
			raise ApiError("Live market rates need an API key (free at https://allratestoday.com/register)")
		wanted = normalise_currencies(currencies)
		payload = get_json(live_url(base, wanted), headers={"Authorization": f"Bearer {api_key}"}, opener=opener)
		result = parse_live(payload, base, wanted)
		result.source_label = "live"
	else:
		raise ApiError(f"Unknown source {source!r}")
	return result


def erpnext_preset(source: str, api_key: str | None = None, other_code: str | None = None) -> dict:
	"""Values for ERPNext's single DocType "Currency Exchange Settings", as
	documented at https://allratestoday.com/migrate/erpnext/.

	Without a key: the keyless ECB endpoint (Option 1). With a key and a
	central-bank source that publishes one rate per pair (RBI, ECB or another
	code): that bank's dated table (Option 2, paid plans). CBIC, bank cards and
	live rates fall back to Option 1 because ERPNext's on-demand lookup can
	only read one number per pair.
	"""
	code = None
	if api_key:
		if source == SOURCE_RBI:
			code = "rbi"
		elif source == SOURCE_ECB:
			code = "ecb"
		elif source == SOURCE_OTHER and other_code:
			code = normalise_code(other_code)
	if code:
		return {
			"service_provider": "Custom",
			"disabled": 0,
			"use_http": 0,
			"api_endpoint": CB_PRESET_ENDPOINT.replace("{bank}", code),
			"access_key": "",
			"req_params": [
				{"key": "source", "value": "{from_currency}"},
				{"key": "target", "value": "{to_currency}"},
				{"key": "api_key", "value": api_key},
			],
			"result_key": [{"key": "rate"}],
		}
	return {
		"service_provider": "Custom",
		"disabled": 0,
		"use_http": 0,
		"api_endpoint": ECB_PRESET_ENDPOINT,
		"access_key": "",
		"req_params": [
			{"key": "base", "value": "{from_currency}"},
			{"key": "symbols", "value": "{to_currency}"},
		],
		"result_key": [{"key": "rates"}, {"key": "{to_currency}"}],
	}
