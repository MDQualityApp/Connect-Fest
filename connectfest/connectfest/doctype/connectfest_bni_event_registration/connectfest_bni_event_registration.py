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

		# Auto-generate or regenerate official Event Pass Badge card image
		doc_before = self.get_doc_before_save()
		needs_regen = (
			not getattr(self, "event_pass_url", None)
			or not doc_before
			or doc_before.full_name != self.full_name
			or doc_before.company_name != self.company_name
			or doc_before.chapter_name != self.chapter_name
			or doc_before.mobile_no != self.mobile_no
		)
		if needs_regen and (getattr(self, "pass_code", None) or getattr(self, "registration_id", None) or self.name):
			try:
				from connectfest.services.event_badge import generate_event_badge_image
				b_url = generate_event_badge_image(self)
				if b_url:
					self.event_pass_url = b_url
			except Exception:
				pass

