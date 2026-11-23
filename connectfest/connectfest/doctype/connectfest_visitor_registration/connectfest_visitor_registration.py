# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ConnectFestVisitorRegistration(Document):
	def before_insert(self):
		if not self.registration_id and self.name:
			self.registration_id = self.name

	def validate(self):
		if not getattr(self, "visitor_image_pass", None) and (getattr(self, "pass_code", None) or getattr(self, "registration_id", None) or self.name):
			try:
				from connectfest.services.visitor_badge import generate_visitor_badge_image
				badge_url = generate_visitor_badge_image(self)
				if badge_url:
					self.visitor_image_pass = badge_url
			except Exception:
				pass
