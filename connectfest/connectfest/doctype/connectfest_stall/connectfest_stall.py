# Copyright (c) 2026, ConnectFest and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


from frappe.utils import get_datetime, now_datetime


class ConnectFestStall(Document):
	def onload(self):
		self.check_and_release_expired_hold(is_validating=False)

	def validate(self):
		self.check_and_release_expired_hold(is_validating=True)

	def check_and_release_expired_hold(self, is_validating=False):
		"""
		Automatically detects if a 10-minute hold has expired when loading or saving in Desk/API.
		Immediately resets the stall to Available and clears holding attendee data.
		"""
		if self.status == "Hold":
			try:
				from connectfest.services.common import STALL_LOCK_PREFIX
				cache = frappe.cache()
				raw_key = cache.make_key(f"{STALL_LOCK_PREFIX}{self.stall_number or self.name}")
				redis_val = None
				redis_error = False
				try:
					redis_val = cache.get(raw_key)
				except Exception:
					redis_error = True

				is_expired = False
				if redis_error:
					if self.hold_expires_at and get_datetime(self.hold_expires_at) <= now_datetime():
						is_expired = True
				else:
					if not redis_val: # Redis TTL expired (10 minutes up)!
						is_expired = True
					elif self.hold_expires_at and get_datetime(self.hold_expires_at) <= now_datetime():
						is_expired = True

				if is_expired:
					self.status = "Available"
					self.hold_token = None
					self.hold_expires_at = None
					self.booked_by_registration = None
					self.booked_by_name = None
					self.booked_by_company = None
					self.booked_by_email = None
					self.booked_by_mobile = None
					self.payment_mode = None

					if not is_validating:
						frappe.db.set_value("ConnectFest Stall", self.name, {
							"status": "Available",
							"hold_token": None,
							"hold_expires_at": None,
							"booked_by_registration": None,
							"booked_by_name": None,
							"booked_by_company": None,
							"booked_by_email": None,
							"booked_by_mobile": None,
							"payment_mode": None,
						}, update_modified=False)
						frappe.db.commit()

					try:
						from connectfest.services.common import STALL_LOCK_PREFIX
						cache = frappe.cache()
						cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{self.stall_number or self.name}"))
						from connectfest.services.stalls import invalidate_stalls_cache
						invalidate_stalls_cache()
						from connectfest.services.realtime import broadcast_stall_update
						broadcast_stall_update(
							self.stall_number or self.name,
							"Available",
							{
								"tier": self.tier,
								"price": self.price,
								"dimensions": self.dimensions,
								"is_blocked_by_admin": bool(self.is_blocked_by_admin),
								"blocked_reason": self.blocked_reason,
								"booked_by_company": None,
								"booked_by_name": None,
								"hold_expires_at": None,
							}
						)
					except Exception:
						pass
			except Exception:
				pass

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
