# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ConnectFestTermsandConditions(Document):
	def validate(self):
		if self.title:
			self.title = self.title.strip()
		if self.terms_type:
			self.terms_type = self.terms_type.strip()
