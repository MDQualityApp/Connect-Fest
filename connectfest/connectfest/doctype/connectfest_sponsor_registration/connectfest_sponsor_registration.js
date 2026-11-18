// Copyright (c) 2026, MDQuality Apps and contributors
// For license information, please see license.txt

frappe.ui.form.on("ConnectFest Sponsor Registration", {
	refresh(frm) {
		if (frm.doc.status === "Pending") {
			frm.add_custom_button(__("Accept"), () => {
				frappe.confirm(__("Are you sure you want to accept this sponsor application?"), () => {
					frappe.call({
						method: "connectfest.api.admin_accept_sponsor_registration",
						args: { registration_id: frm.doc.name },
						callback(r) {
							if (r && !r.exc) {
								frappe.show_alert({ message: __("Sponsor Accepted and Added!"), indicator: "green" });
								frm.reload_doc();
							}
						}
					});
				});
			}, __("Actions")).addClass("btn-success");

			frm.add_custom_button(__("Reject"), () => {
				frappe.confirm(__("Are you sure you want to reject this sponsor application?"), () => {
					frappe.call({
						method: "connectfest.api.admin_reject_sponsor_registration",
						args: { registration_id: frm.doc.name },
						callback(r) {
							if (r && !r.exc) {
								frappe.show_alert({ message: __("Sponsor Request Rejected"), indicator: "orange" });
								frm.reload_doc();
							}
						}
					});
				});
			}, __("Actions")).addClass("btn-warning");

			frm.add_custom_button(__("Block IP"), () => {
				frappe.confirm(
					__("Are you sure you want to block IP {0}? No more requests will be allowed from this IP.", [frm.doc.ip_address || "this client"]),
					() => {
						frappe.call({
							method: "connectfest.api.admin_block_sponsor_registration",
							args: { registration_id: frm.doc.name },
							callback(r) {
								if (r && !r.exc) {
									frappe.show_alert({ message: __("Sponsor Registration & IP Blocked!"), indicator: "red" });
									frm.reload_doc();
								}
							}
						});
					}
				);
			}, __("Actions")).addClass("btn-danger");
		}
	}
});
