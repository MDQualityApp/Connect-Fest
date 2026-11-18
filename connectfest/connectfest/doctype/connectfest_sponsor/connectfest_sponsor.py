# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint


class ConnectFestSponsor(Document):
	def validate(self):
		if self.sponsor_name:
			self.sponsor_name = self.sponsor_name.strip()
		if self.sponsor_category:
			self.sponsor_category = self.sponsor_category.strip()
		if self.sponsored_for:
			self.sponsored_for = self.sponsored_for.strip()
		if getattr(self, "contact_person", None):
			self.contact_person = str(self.contact_person).strip()
		if getattr(self, "email", None):
			self.email = str(self.email).strip().lower()
		if getattr(self, "phone", None):
			self.phone = str(self.phone).strip()

		# Ensure Top Banner uniqueness: exactly 1 top banner sponsor allowed at a time (no duplicates)
		if cint(self.is_featured) == 1:
			frappe.db.sql(
				"UPDATE `tabConnectFest Sponsor` SET is_featured = 0 WHERE name != %s AND is_featured = 1",
				(self.name or "",)
			)

	def on_update(self):
		try:
			from connectfest.services.sponsors import invalidate_sponsors_cache
			invalidate_sponsors_cache()
		except Exception:
			pass

	def on_trash(self):
		try:
			from connectfest.services.sponsors import invalidate_sponsors_cache
			invalidate_sponsors_cache()
		except Exception:
			pass
