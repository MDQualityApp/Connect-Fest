# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ConnectFestSponsor(Document):
	def validate(self):
		if self.sponsor_name:
			self.sponsor_name = self.sponsor_name.strip()
		if self.sponsor_category:
			self.sponsor_category = self.sponsor_category.strip()
		if self.sponsored_for:
			self.sponsored_for = self.sponsored_for.strip()
