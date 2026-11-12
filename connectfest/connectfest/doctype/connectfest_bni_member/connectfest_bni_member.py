# Copyright (c) 2026, ConnectFest and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
import re


def normalize_bni_phone(raw_phone):
	"""
	Strips all non-digit characters, country codes (+91, 91), and leading zeros.
	Returns standard 10-digit number.
	"""
	if not raw_phone:
		return ""
	digits = re.sub(r"\D", "", str(raw_phone).strip())
	if digits.startswith("91") and len(digits) == 12:
		digits = digits[2:]
	elif digits.startswith("0") and len(digits) > 10:
		digits = digits.lstrip("0")
	if len(digits) > 10:
		digits = digits[-10:]
	return digits


class ConnectFestBNIMember(Document):
	def validate(self):
		cleaned = normalize_bni_phone(self.mobile_no)
		if not cleaned or len(cleaned) != 10:
			frappe.throw("A valid 10-digit Mobile Number is required for BNI Member.")
		self.mobile_no = cleaned

	def after_insert(self):
		self.sync_registrations(is_bni=1)

	def on_update(self):
		self.sync_registrations(is_bni=1)

	def on_trash(self):
		self.sync_registrations(is_bni=0)

	def sync_registrations(self, is_bni=1):
		try:
			from connectfest.services.bni_members import clear_bni_member_cache
			clear_bni_member_cache()
			if self.mobile_no:
				frappe.db.sql("""
					UPDATE `tabConnectFest Registration`
					SET is_bni_member = %(is_bni)s
					WHERE mobile_no = %(mob)s OR RIGHT(mobile_no, 10) = %(mob)s
				""", {"mob": self.mobile_no, "is_bni": is_bni})
			if self.email:
				frappe.db.sql("""
					UPDATE `tabConnectFest Registration`
					SET is_bni_member = %(is_bni)s
					WHERE email = %(email)s
				""", {"email": self.email, "is_bni": is_bni})
		except Exception:
			pass
