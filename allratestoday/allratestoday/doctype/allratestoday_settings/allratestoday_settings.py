# Copyright (c) 2026, AllRatesToday and contributors
# For license information, please see LICENSE

import frappe
from frappe import _
from frappe.model.document import Document

from allratestoday import api, sync


class AllRatesTodaySettings(Document):
	def validate(self):
		if self.source == api.SOURCE_OTHER:
			try:
				self.other_bank_code = api.normalise_code(self.other_bank_code)
			except api.ApiError as e:
				frappe.throw(str(e))
		if self.source == api.SOURCE_LIVE and not self.api_key:
			frappe.throw(_("Live market rates need an API key. Get a free API key at https://allratestoday.com/register"))
		seen = set()
		for row in self.get("currencies") or []:
			if row.currency in seen:
				frappe.throw(_("Currency {0} is listed twice").format(row.currency))
			seen.add(row.currency)


@frappe.whitelist()
def sync_now():
	return sync.sync_now()


@frappe.whitelist()
def apply_erpnext_preset():
	return sync.apply_erpnext_preset()
