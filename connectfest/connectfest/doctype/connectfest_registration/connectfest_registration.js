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
		const show_bni_fields = (frm.doc.claimed_bni_member || frm.doc.is_bni_member) ? true : false;
		frm.toggle_display(["region_name", "chapter_name", "industry", "segment"], show_bni_fields);
	},
	stall_category(frm) {
		const price = STALL_PRICING[frm.doc.stall_category] || 25000;
		frm.set_value("amount", price);
	},
	claimed_bni_member(frm) {
		const show_bni_fields = (frm.doc.claimed_bni_member || frm.doc.is_bni_member) ? true : false;
		frm.toggle_display(["region_name", "chapter_name", "industry", "segment"], show_bni_fields);
	},
	is_bni_member(frm) {
		const show_bni_fields = (frm.doc.claimed_bni_member || frm.doc.is_bni_member) ? true : false;
		frm.toggle_display(["region_name", "chapter_name", "industry", "segment"], show_bni_fields);
		frm.toggle_reqd(
			["region_name", "chapter_name"],
			frm.doc.is_bni_member ? true : false
		);
	}
});
