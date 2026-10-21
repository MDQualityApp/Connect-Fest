# Copyright (c) 2026, MDQuality Apps and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ConnectFestGallery(Document):
	def validate(self):
		"""
		If cover_image is not explicitly set, default to the first image from the child table.
		"""
		if not self.cover_image and getattr(self, "images", None) and len(self.images) > 0:
			self.cover_image = self.images[0].image
