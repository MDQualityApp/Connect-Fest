// Copyright (c) 2026, MDQuality Apps and contributors
// For license information, please see license.txt

frappe.ui.form.on('ConnectFest Stall', {
	refresh: function(frm) {
		frm.add_custom_button(__('Bulk Create Stalls'), function() {
			if (window.open_connectfest_bulk_create_dialog) {
				window.open_connectfest_bulk_create_dialog(frm);
			}
		});
	}
});
