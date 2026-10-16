# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ConnectFestVisitorRegistration(Document):
	def before_insert(self):
		if not self.registration_id and self.name:
			self.registration_id = self.name
