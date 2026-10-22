# Copyright (c) 2026, ConnectFest and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ConnectFestStall(Document):
	def on_update(self):
		try:
			from connectfest.services.realtime import broadcast_stall_update
			broadcast_stall_update(
				self.stall_number or self.name,
				self.status or "Available",
				{
					"tier": self.tier,
					"price": self.price,
					"dimensions": self.dimensions,
					"is_blocked_by_admin": bool(self.is_blocked_by_admin),
					"blocked_reason": self.blocked_reason,
					"booked_by_company": self.booked_by_company,
					"booked_by_name": self.booked_by_name,
					"hold_expires_at": str(self.hold_expires_at) if self.hold_expires_at else None,
				}
			)
		except Exception:
			pass
