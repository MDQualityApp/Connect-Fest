import frappe
from frappe.utils import flt
from connectfest.services.common import extract_request_data


def get_tier_pricing():
	"""Fetch dynamic stall pricing from ConnectFest Settings DocType."""
	try:
		settings = frappe.get_single("ConnectFest Settings")
		return {
			"Regular": flt(settings.price_regular or 25000.0),
			"Standard": flt(settings.price_standard or 35000.0),
			"Premium": flt(settings.price_premium or 45000.0),
			"Silver": flt(settings.price_silver or 100000.0),
		}
	except Exception:
		return {
			"Regular": 25000.0,
			"Standard": 35000.0,
			"Premium": 45000.0,
			"Silver": 100000.0,
		}


class DynamicStallPricing(dict):
	"""Dynamic dictionary that queries ConnectFest Settings in real-time."""
	def _data(self):
		return get_tier_pricing()
	def __getitem__(self, key):
		return self._data()[key]
	def get(self, key, default=None):
		return self._data().get(key, default)
	def __contains__(self, key):
		return key in self._data()
	def keys(self):
		return self._data().keys()
	def values(self):
		return self._data().values()
	def items(self):
		return self._data().items()
	def copy(self):
		return self._data()


STALL_PRICING = DynamicStallPricing()


def update_dynamic_pricing(silver=None, premium=None, standard=None, regular=None, **kwargs):
	"""
	Admin API to dynamically update stall pricing in ConnectFest Settings and sync available stalls.
	"""
	try:
		data = extract_request_data(kwargs)
		s = flt(silver or data.get("silver") or 0)
		p = flt(premium or data.get("premium") or 0)
		st = flt(standard or data.get("standard") or 0)
		r = flt(regular or data.get("regular") or 0)

		settings = frappe.get_single("ConnectFest Settings")
		if s > 0: settings.price_silver = s
		if p > 0: settings.price_premium = p
		if st > 0: settings.price_standard = st
		if r > 0: settings.price_regular = r
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		# Sync new prices to all available stalls in the floor map
		if s > 0:
			frappe.db.sql("UPDATE `tabConnectFest Stall` SET price = %s WHERE tier = 'Silver' AND status = 'Available'", (s,))
		if p > 0:
			frappe.db.sql("UPDATE `tabConnectFest Stall` SET price = %s WHERE tier = 'Premium' AND status = 'Available'", (p,))
		if st > 0:
			frappe.db.sql("UPDATE `tabConnectFest Stall` SET price = %s WHERE tier = 'Standard' AND status = 'Available'", (st,))
		if r > 0:
			frappe.db.sql("UPDATE `tabConnectFest Stall` SET price = %s WHERE tier = 'Regular' AND status = 'Available'", (r,))
		frappe.db.commit()

		return {
			"status": "success",
			"message": "Pricing updated dynamically by Admin and synced to all available stalls",
			"pricing": get_tier_pricing(),
		}
	except Exception as e:
		frappe.log_error("ConnectFest Dynamic Pricing Update Error", frappe.get_traceback())
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}
