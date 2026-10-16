# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ConnectFestRegistration(Document):
	def before_insert(self):
		if not self.registration_id and self.name:
			self.registration_id = self.name

	def validate(self):
		if self.is_bni_member:
			for field in ["region_name", "chapter_name"]:
				if not self.get(field):
					frappe.throw(frappe._("{0} is mandatory for BNI Members").format(self.meta.get_label(field)))
