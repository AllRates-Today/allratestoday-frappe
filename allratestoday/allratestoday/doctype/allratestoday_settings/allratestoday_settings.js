// Copyright (c) 2026, AllRatesToday and contributors
// For license information, please see LICENSE

const ART_METHOD = "allratestoday.allratestoday.doctype.allratestoday_settings.allratestoday_settings.";

frappe.ui.form.on("AllRatesToday Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Sync now"), () => {
			const run = () =>
				frappe.call({
					method: ART_METHOD + "sync_now",
					freeze: true,
					freeze_message: __("Fetching rates from AllRatesToday..."),
					callback(r) {
						const s = r.message || {};
						const msg = __("Created {0}, updated {1}, unchanged {2}. Rate date: {3}.", [
							s.created || 0,
							s.updated || 0,
							s.unchanged || 0,
							s.rate_date || "-",
						]);
						frappe.msgprint({
							title: s.error ? __("Synced with notes") : __("Synced"),
							message: s.error ? msg + "<br><br>" + frappe.utils.escape_html(s.error) : msg,
							indicator: s.error ? "orange" : "green",
						});
						frm.reload_doc();
					},
				});
			frm.is_dirty() ? frm.save().then(run) : run();
		});

		frm.add_custom_button(__("Apply ERPNext provider preset"), () => {
			frappe.confirm(
				__(
					"This overwrites Currency Exchange Settings so that ERPNext's on-demand rate lookups also use AllRatesToday. ERPNext will test the settings with a USD to INR request. Continue?"
				),
				() =>
					frappe.call({
						method: ART_METHOD + "apply_erpnext_preset",
						freeze: true,
						callback(r) {
							const m = r.message || {};
							frappe.msgprint({
								title: __("Currency Exchange Settings updated"),
								message: __("Endpoint: {0}", [frappe.utils.escape_html(m.api_endpoint || "")]),
								indicator: "green",
							});
						},
					})
			);
		});
	},
});
