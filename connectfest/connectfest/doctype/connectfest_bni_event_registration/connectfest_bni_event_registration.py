# Copyright (c) 2026, ConnectFest and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
import re


def normalize_bni_phone(raw_phone):
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


class ConnectFestBNIEventRegistration(Document):
	def before_insert(self):
		if not self.registration_id and self.name:
			self.registration_id = self.name
		if not self.pass_code:
			rand_code = frappe.generate_hash(length=8).upper()
			self.pass_code = f"BNI-PASS-{rand_code}"
		if not self.qr_payload:
			self.qr_payload = frappe.as_json({
				"type": "bni_event_pass",
				"registration_id": self.registration_id or self.name,
				"pass_code": self.pass_code,
				"name": self.full_name,
				"company": self.company_name or "",
				"chapter": self.chapter_name or "",
				"mobile": self.mobile_no,
			})

	def validate(self):
		self.mobile_no = normalize_bni_phone(self.mobile_no)
		if not self.mobile_no or len(self.mobile_no) != 10:
			frappe.throw("A valid 10-digit Mobile Number is required.")
