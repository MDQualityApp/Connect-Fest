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
		self.set_pricing()
		if self.is_bni_member:
			for field in ["region_name", "chapter_name"]:
				if not self.get(field):
					frappe.throw(frappe._("{0} is mandatory for BNI Members").format(self.meta.get_label(field)))

	def set_pricing(self):
		if not self.stall_category:
			self.stall_category = "Regular"
		self.amount = STALL_PRICING.get(self.stall_category, 25000.0)
		if not self.payment_status:
			self.payment_status = "Pending"
