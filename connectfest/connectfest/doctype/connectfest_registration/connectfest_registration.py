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
		# Prevent cross-account conflict, but allow multiple stall bookings for the same user
		if self.email or self.mobile_no:
			existing = frappe.db.sql("""
				SELECT name, registration_id, email, mobile_no, stall_number, payment_status
				FROM `tabConnectFest Registration`
				WHERE (email = %(email)s OR mobile_no = %(mobile)s)
				  AND name != %(name)s
			""", {"email": self.email, "mobile": self.mobile_no, "name": self.name or ""}, as_dict=True)
			if existing:
				for ex in existing:
					ex_email = (ex.get("email") or "").strip().lower()
					ex_mob = (ex.get("mobile_no") or "").strip()
					req_email = (self.email or "").strip().lower()
					req_mob = (self.mobile_no or "").strip()

					# Conflict A: Mobile already used by another email
					if req_mob and ex_mob == req_mob and ex_email != req_email:
						frappe.throw(frappe._("Mobile number '{0}' is already registered with another account ({1})!").format(req_mob, ex_email))

					# Conflict B: Email already used by another mobile
					if req_email and ex_email == req_email and req_mob and ex_mob != req_mob:
						frappe.throw(frappe._("Email address '{0}' is already registered with mobile number {1}!").format(req_email, ex_mob))

					# If same stall already booked and paid by this user
					if self.stall_number and ex.get("stall_number") == self.stall_number and ex.get("payment_status") == "Paid":
						frappe.throw(frappe._("Stall '{0}' is already booked and paid for (Registration ID: {1}).").format(self.stall_number, ex.get("registration_id") or ex.get("name")))

		if self.payment_mode and self.payment_mode.lower() in ("cash", "cheque", "bank transfer"):
			sess_user = getattr(frappe.session, "user", None)
			from connectfest.services.auth import is_admin_user
			if sess_user and sess_user != "Administrator" and not is_admin_user(sess_user):
				frappe.throw(frappe._("Cash payments can only be processed and recorded by Event Administration at the venue desk. Exhibitors must pay online via Razorpay."))

		if self.payment_status == "Paid":
			if not self.stall_number:
				frappe.throw(frappe._("Stall Number (stall_number) is mandatory to confirm payment! Please select a valid stall."))

		if self.stall_number:
			if not frappe.db.exists("ConnectFest Stall", self.stall_number):
				frappe.throw(frappe._("Stall '{0}' does not exist in ConnectFest inventory").format(self.stall_number))

			stall = frappe.get_doc("ConnectFest Stall", self.stall_number)
			sess_u = getattr(frappe.session, "user", None)
			from connectfest.services.auth import is_admin_user
			if stall.is_blocked_by_admin and not is_admin_user(sess_u):
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
			if not self.payment_status:
				self.payment_status = "Pending"
		elif self.stall_category and self.stall_category.strip():
			from connectfest.services.pricing import get_tier_pricing
			dynamic_pricing = get_tier_pricing()
			self.amount = dynamic_pricing.get(self.stall_category, 0.0)
			if not self.payment_status:
				self.payment_status = "Pending"
		else:
			self.stall_category = ""
			self.amount = 0.0
			if self.payment_status == "Pending" and not self.stall_number:
				self.payment_status = ""
			if not self.stall_number:
				self.payment_mode = self.payment_mode or ""

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
					stall.payment_mode = getattr(self, "payment_mode", None) or "Online"
					stall.hold_token = None
					stall.hold_expires_at = None
					stall.save(ignore_permissions=True)
					frappe.cache().delete(frappe.cache().make_key(f"connectfest:stall_lock:{self.stall_number}"))

	def on_trash(self):
		"""When a registration document is deleted, release its allocated stall back to Available."""
		if self.stall_number and frappe.db.exists("ConnectFest Stall", self.stall_number):
			try:
				stall = frappe.get_doc("ConnectFest Stall", self.stall_number)
				reg_id = self.registration_id or self.name
				if stall.booked_by_registration == reg_id or stall.booked_by_email == self.email:
					stall.status = "Available"
					stall.booked_by_registration = None
					stall.booked_by_name = None
					stall.booked_by_company = None
					stall.booked_by_email = None
					stall.booked_by_mobile = None
					stall.hold_token = None
					stall.hold_expires_at = None
					stall.payment_mode = None
					stall.save(ignore_permissions=True)
			except Exception:
				pass

			try:
				cache = frappe.cache()
				cache.delete(cache.make_key(f"connectfest:stall_lock:{self.stall_number}"))
				cache.delete_keys(f"cf:stall_lock:{self.stall_number}*")
			except Exception:
				pass


def on_registration_trash(doc, method=None):
	"""Module hook callable from doc_events."""
	doc.on_trash()

