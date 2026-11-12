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
