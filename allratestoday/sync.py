"""Frappe glue: read AllRatesToday Settings, fetch rates, upsert Currency Exchange.

All ``frappe`` imports are inside functions so ``allratestoday.api`` and this
module can be imported without a bench (tests only exercise ``api``).
"""

from __future__ import annotations

from allratestoday import api

SETTINGS = "AllRatesToday Settings"
MANAGER_ROLES = ("System Manager", "Accounts Manager")
MAX_ERROR_LEN = 2000


# ------------------------------------------------------------------ scheduler


def scheduled_daily():
	"""Daily job: keyless sources, and live rates when set to daily."""
	settings = _settings_or_none()
	if not settings or not settings.enabled:
		return
	if settings.source == api.SOURCE_LIVE and (settings.live_sync_frequency or "Daily") == "Hourly":
		return
	run_sync(settings)


def scheduled_hourly():
	"""Hourly job: live market rates only, when set to hourly."""
	settings = _settings_or_none()
	if not settings or not settings.enabled:
		return
	if settings.source == api.SOURCE_LIVE and settings.live_sync_frequency == "Hourly":
		run_sync(settings)


def _settings_or_none():
	import frappe

	try:
		return frappe.get_single(SETTINGS)
	except Exception:
		frappe.log_error(title="AllRatesToday: cannot load settings", message=frappe.get_traceback())
		return None


# ---------------------------------------------------------------- whitelisted


def sync_now():
	import frappe

	frappe.only_for(MANAGER_ROLES)
	summary = run_sync(frappe.get_single(SETTINGS))
	return summary


def apply_erpnext_preset():
	"""Point ERPNext's Currency Exchange Settings at AllRatesToday so that
	on-demand lookups (pairs or dates without a Currency Exchange record) are
	answered by the same provider. Saving runs ERPNext's own USD->INR check."""
	import frappe

	frappe.only_for(MANAGER_ROLES)
	settings = frappe.get_single(SETTINGS)
	preset = api.erpnext_preset(settings.source, _api_key(settings), settings.other_bank_code)

	ces = frappe.get_single("Currency Exchange Settings")
	for field in ("service_provider", "disabled", "use_http", "api_endpoint", "access_key"):
		if ces.meta.has_field(field):
			ces.set(field, preset[field])
	ces.set("req_params", [])
	for row in preset["req_params"]:
		ces.append("req_params", row)
	ces.set("result_key", [])
	for row in preset["result_key"]:
		ces.append("result_key", row)
	ces.save()
	return {"api_endpoint": ces.api_endpoint, "url": ces.get("url")}


# ------------------------------------------------------------------------ core


def run_sync(settings) -> dict:
	"""Fetch and upsert. Never raises: errors land in last_error and the Error Log."""
	import frappe
	from frappe.utils import now_datetime

	summary = {"created": 0, "updated": 0, "unchanged": 0, "skipped": [], "missing": [], "rate_date": None}
	error = ""
	try:
		base = (settings.base_currency or _company_currency() or "INR").upper()
		requested = _requested_currencies(settings)
		result = api.fetch_rates(
			settings.source,
			base=base,
			currencies=requested,
			split=bool(settings.write_buying_selling),
			api_key=_api_key(settings),
			bank=settings.bank or "sbi",
			other_code=settings.other_bank_code,
		)
		summary["rate_date"] = result.rate_date
		summary["missing"] = result.missing

		known = set(frappe.get_all("Currency", pluck="name"))
		enabled = set(frappe.get_all("Currency", filters={"enabled": 1}, pluck="name"))
		if base not in known:
			raise api.ApiError(f"Base currency {base} does not exist in the Currency list")

		for rate in result.rates:
			# Explicit list: skip only codes ERPNext lacks. Empty list ("all"):
			# keep it to currencies enabled on this site.
			allowed = known if requested else enabled
			if rate.from_currency not in allowed:
				if rate.from_currency not in summary["skipped"]:
					summary["skipped"].append(rate.from_currency)
				continue
			summary[_upsert(rate, bool(settings.write_inverse))] += 1

		notes = []
		if result.missing:
			notes.append(f"Not published by {result.source_label}: {', '.join(result.missing)}")
		if summary["skipped"]:
			notes.append(f"Not in ERPNext Currency list (or disabled): {', '.join(summary['skipped'])}")
		error = "; ".join(notes)
	except Exception as e:  # never raise inside the scheduler
		frappe.db.rollback()
		error = str(e) if isinstance(e, api.ApiError) else f"{type(e).__name__}: {e}"
		frappe.log_error(title="AllRatesToday sync failed", message=frappe.get_traceback())

	_set_single("last_sync", now_datetime())
	_set_single("last_error", error[:MAX_ERROR_LEN])
	if summary["rate_date"]:
		_set_single("last_rate_date", summary["rate_date"])
	frappe.db.commit()
	summary["error"] = error
	return summary


def _upsert(rate: api.Rate, write_inverse: bool) -> str:
	outcome = _upsert_one(rate.date, rate.from_currency, rate.to_currency, rate.rate, rate.applies_to)
	if write_inverse and rate.rate:
		_upsert_one(rate.date, rate.to_currency, rate.from_currency, round(1.0 / rate.rate, api.PRECISION), rate.applies_to)
	return outcome


def _flags(applies_to: str):
	if applies_to == api.BUYING:
		return 1, 0
	if applies_to == api.SELLING:
		return 0, 1
	return 1, 1


def _record_name(date: str, from_currency: str, to_currency: str, for_buying: int, for_selling: int) -> str:
	# Mirrors CurrencyExchange.autoname in ERPNext.
	purpose = "Selling-Buying"
	if not for_buying and for_selling:
		purpose = "Selling"
	elif for_buying and not for_selling:
		purpose = "Buying"
	return f"{date}-{from_currency}-{to_currency}-{purpose}"


def _upsert_one(date, from_currency, to_currency, value, applies_to) -> str:
	import frappe
	from frappe.utils import flt

	for_buying, for_selling = _flags(applies_to)
	name = _record_name(date, from_currency, to_currency, for_buying, for_selling)

	# A combined record and a split pair for the same day would both match a
	# for_buying lookup; keep only the shape the current settings produce.
	if applies_to == api.BOTH:
		stale = [_record_name(date, from_currency, to_currency, 1, 0), _record_name(date, from_currency, to_currency, 0, 1)]
	else:
		stale = [_record_name(date, from_currency, to_currency, 1, 1)]
	for other in stale:
		if frappe.db.exists("Currency Exchange", other):
			frappe.delete_doc("Currency Exchange", other, ignore_permissions=True, force=True)

	if frappe.db.exists("Currency Exchange", name):
		doc = frappe.get_doc("Currency Exchange", name)
		if flt(doc.exchange_rate, api.PRECISION) == flt(value, api.PRECISION):
			return "unchanged"
		doc.exchange_rate = value
		doc.save(ignore_permissions=True)
		return "updated"

	frappe.get_doc(
		{
			"doctype": "Currency Exchange",
			"date": date,
			"from_currency": from_currency,
			"to_currency": to_currency,
			"exchange_rate": value,
			"for_buying": for_buying,
			"for_selling": for_selling,
		}
	).insert(ignore_permissions=True)
	return "created"


# --------------------------------------------------------------------- helpers


def _requested_currencies(settings) -> list:
	rows = settings.get("currencies") or []
	return api.normalise_currencies([r.currency for r in rows if r.get("currency")])


def _api_key(settings):
	if settings.source != api.SOURCE_LIVE and not settings.get("api_key"):
		return None
	try:
		return settings.get_password("api_key", raise_exception=False) or None
	except Exception:
		return None


def _company_currency():
	import frappe

	company = frappe.defaults.get_global_default("company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)
	if company:
		return frappe.get_cached_value("Company", company, "default_currency")
	return None


def _set_single(field, value):
	import frappe

	if hasattr(frappe.db, "set_single_value"):
		frappe.db.set_single_value(SETTINGS, field, value)
	else:  # older Frappe
		frappe.db.set_value(SETTINGS, None, field, value)
