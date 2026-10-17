# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

STALL_PRICING = {
	"Regular": 25000.0,
	"Standard": 35000.0,
	"Premium": 45000.0,
	"Silver": 100000.0,
}


class ConnectFestRegistration(Document):
	def before_insert(self):
		if not self.registration_id and self.name:
			self.registration_id = self.name
		self.set_pricing()

	def validate(self):
		# Strict Uniqueness on Email and Mobile
		if self.email or self.mobile_no:
			existing = frappe.db.sql("""
				SELECT name, registration_id FROM `tabConnectFest Registration`
				WHERE (email = %(email)s OR mobile_no = %(mobile)s)
				  AND name != %(name)s
				LIMIT 1
			""", {"email": self.email, "mobile": self.mobile_no, "name": self.name or ""}, as_dict=True)
			if existing:
				frappe.throw(frappe._("An exhibitor registration already exists with this Email or Mobile Number (Registration ID: {0}).").format(existing[0].registration_id or existing[0].name))

		if self.payment_status == "Paid":
			if not self.stall_number:
				frappe.throw(frappe._("Stall Number (stall_number) is mandatory to confirm payment! Please select a valid stall."))

		if self.stall_number:
			if not frappe.db.exists("ConnectFest Stall", self.stall_number):
				frappe.throw(frappe._("Stall '{0}' does not exist in ConnectFest inventory").format(self.stall_number))

			stall = frappe.get_doc("ConnectFest Stall", self.stall_number)
			if stall.is_blocked_by_admin:
				frappe.throw(frappe._("Stall '{0}' is blocked by administration ({1})").format(self.stall_number, stall.blocked_reason or "Reserved"))

			if stall.status == "Booked" and stall.booked_by_registration and stall.booked_by_registration != (self.registration_id or self.name):
				frappe.throw(frappe._("Stall '{0}' is already booked and confirmed by another exhibitor").format(self.stall_number))

		self.set_pricing()

		if self.is_bni_member:
			for field in ["region_name", "chapter_name"]:
				if not self.get(field):
					frappe.throw(frappe._("{0} is mandatory for BNI Members").format(self.meta.get_label(field)))

		if self.email:
			from connectfest.services.auth import ensure_frappe_user
			ensure_frappe_user(self.email, self.full_name, self.mobile_no, role="Website User")

	def set_pricing(self):
		if self.stall_number and frappe.db.exists("ConnectFest Stall", self.stall_number):
			stall_doc = frappe.get_doc("ConnectFest Stall", self.stall_number)
			self.stall_category = stall_doc.tier
			from connectfest.services.pricing import get_tier_pricing
			dynamic_pricing = get_tier_pricing()
			self.amount = dynamic_pricing.get(stall_doc.tier, stall_doc.price)
		elif self.stall_category:
			from connectfest.services.pricing import get_tier_pricing
			dynamic_pricing = get_tier_pricing()
			self.amount = dynamic_pricing.get(self.stall_category, 0.0)
		else:
			self.stall_category = ""
			self.amount = 0.0
		if not self.payment_status:
			self.payment_status = "Pending"

	def on_update(self):
		if self.stall_number and self.payment_status == "Paid":
			if frappe.db.exists("ConnectFest Stall", self.stall_number):
				stall = frappe.get_doc("ConnectFest Stall", self.stall_number)
				if stall.status != "Booked" or stall.booked_by_registration != (self.registration_id or self.name):
					stall.status = "Booked"
					stall.booked_by_registration = self.registration_id or self.name
					stall.booked_by_name = self.full_name
					stall.booked_by_company = self.company_name
					stall.booked_by_email = self.email
					stall.booked_by_mobile = self.mobile_no
					stall.hold_token = None
					stall.hold_expires_at = None
					stall.save(ignore_permissions=True)
					frappe.cache().delete(frappe.cache().make_key(f"connectfest:stall_lock:{self.stall_number}"))

