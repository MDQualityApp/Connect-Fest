# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ConnectFestBlockedIP(Document):
	def before_insert(self):
		if not self.blocked_at:
			self.blocked_at = frappe.utils.now_datetime()
		if not self.blocked_by:
			self.blocked_by = frappe.session.user or "Administrator"

	def on_update(self):
		try:
			from connectfest.services.sponsors import invalidate_blocked_ips_cache
			invalidate_blocked_ips_cache()
		except Exception:
			pass

	def on_trash(self):
		try:
			from connectfest.services.sponsors import invalidate_blocked_ips_cache
			invalidate_blocked_ips_cache()
		except Exception:
			pass
