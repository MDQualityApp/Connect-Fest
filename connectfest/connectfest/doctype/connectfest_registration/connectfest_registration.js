// Copyright (c) 2026, MDQuality Apps and contributors
// For license information, please see license.txt

const STALL_PRICING = {
	"Regular": 25000,
	"Standard": 35000,
	"Premium": 45000,
	"Silver": 100000
};

frappe.ui.form.on("ConnectFest Registration", {
	refresh(frm) {
		if (frm.doc.stall_category && !frm.doc.amount) {
			frm.set_value("amount", STALL_PRICING[frm.doc.stall_category] || 25000);
		}
	},
	stall_category(frm) {
		const price = STALL_PRICING[frm.doc.stall_category] || 25000;
		frm.set_value("amount", price);
	},
	is_bni_member(frm) {
		frm.toggle_reqd(
			["region_name", "chapter_name"],
			frm.doc.is_bni_member ? true : false
		);
	}
});
