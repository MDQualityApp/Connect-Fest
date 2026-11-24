# Copyright (c) 2026, ConnectFest and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ConnectFestTierPricing(Document):
	def validate(self):
		if self.price is not None and self.price < 0:
			frappe.throw("Base Price cannot be negative")
		if self.gst_percent is not None and (self.gst_percent < 0 or self.gst_percent > 100):
			frappe.throw("GST Percentage must be between 0 and 100")

	def on_update(self):
		try:
			from connectfest.services.stalls import invalidate_stalls_cache
			from connectfest.services.pricing import invalidate_pricing_cache
			invalidate_stalls_cache()
			invalidate_pricing_cache()

			# 1. Sync available stalls of this tier
			if self.tier_name and self.price is not None:
				frappe.db.set_value("ConnectFest Stall", {"tier": self.tier_name, "status": "Available"}, "price", self.price, update_modified=False)
		except Exception:
			pass
