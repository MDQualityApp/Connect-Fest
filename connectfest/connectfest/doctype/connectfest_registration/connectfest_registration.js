// Copyright (c) 2026, MDQuality Apps and contributors
// For license information, please see license.txt

frappe.ui.form.on("ConnectFest Registration", {
	refresh(frm) {
		// Form load logic
	},
	is_bni_member(frm) {
		frm.toggle_reqd(
			["region_name", "chapter_name"],
			frm.doc.is_bni_member ? true : false
		);
	}
});
