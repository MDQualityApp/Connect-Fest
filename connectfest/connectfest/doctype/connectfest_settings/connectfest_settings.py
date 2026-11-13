# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ConnectFestSettings(Document):
	def on_update(self):
		try:
			from connectfest.services.stalls import invalidate_stalls_cache
			from connectfest.services.pricing import invalidate_pricing_cache
			from connectfest.services.bni_members import clear_bni_member_cache
			invalidate_stalls_cache()
			invalidate_pricing_cache()
			clear_bni_member_cache()
			frappe.cache().delete_value("connectfest:settings:stall_hold_minutes")
			frappe.cache().delete_value("connectfest:settings:bni_verification")
		except Exception:
			pass
