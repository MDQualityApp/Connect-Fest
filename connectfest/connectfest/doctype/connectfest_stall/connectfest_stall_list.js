// Copyright (c) 2026, MDQuality Apps and contributors
// For license information, please see license.txt

frappe.listview_settings['ConnectFest Stall'] = {
	add_fields: ['block', 'dimensions', 'price', 'tier', 'status', 'crn'],
	get_indicator: function(doc) {
		if (doc.status === 'Available') {
			return [__('Available'), 'green', 'status,=,Available'];
		} else if (doc.status === 'Booked') {
			return [__('Booked'), 'blue', 'status,=,Booked'];
		} else if (doc.status === 'Hold') {
			return [__('Hold'), 'orange', 'status,=,Hold'];
		} else if (doc.status === 'Blocked') {
			return [__('Blocked'), 'red', 'status,=,Blocked'];
		}
	},
	onload: function(listview) {
		listview.page.add_inner_button(__('Bulk Create Stalls'), function() {
			window.open_connectfest_bulk_create_dialog(listview);
		});
	}
};

window.open_connectfest_bulk_create_dialog = function(caller) {
	let d = new frappe.ui.Dialog({
		title: __('Bulk Create Stalls'),
		fields: [
			{
				label: __('Block / Aisle'),
				fieldname: 'block',
				fieldtype: 'Data',
				placeholder: __('e.g. A, B, C, or Block A')
			},
			{
				label: __('Dimensions'),
				fieldname: 'dimensions',
				fieldtype: 'Data',
				default: '3x2',
				placeholder: __('e.g. 3x2, 3x3, 4x3')
			},
			{
				label: __('Price (INR)'),
				fieldname: 'price',
				fieldtype: 'Currency',
				default: 35000,
				reqd: 1
			},
			{
				fieldtype: 'Column Break'
			},
			{
				label: __('Tier (Optional)'),
				fieldname: 'tier',
				fieldtype: 'Data',
				placeholder: __('Optional: e.g. Standard, Premium')
			},
			{
				label: __('Corner Stall (CRN)'),
				fieldname: 'crn',
				fieldtype: 'Check',
				default: 0
			},
			{
				fieldtype: 'Section Break',
				label: __('Stall Number Generation')
			},
			{
				label: __('Stall Naming Mode'),
				fieldname: 'mode',
				fieldtype: 'Select',
				options: [
					'Sequential Range (Prefix + Count)',
					'Custom Stall Numbers List'
				],
				default: 'Sequential Range (Prefix + Count)'
			},
			{
				label: __('Stall Prefix'),
				fieldname: 'prefix',
				fieldtype: 'Data',
				default: 'A-',
				depends_on: 'eval:doc.mode=="Sequential Range (Prefix + Count)"',
				placeholder: __('e.g. A-, B-, ST-')
			},
			{
				label: __('Start Number'),
				fieldname: 'start_number',
				fieldtype: 'Int',
				default: 1,
				depends_on: 'eval:doc.mode=="Sequential Range (Prefix + Count)"'
			},
			{
				label: __('Number of Stalls (Count)'),
				fieldname: 'count',
				fieldtype: 'Int',
				default: 5,
				depends_on: 'eval:doc.mode=="Sequential Range (Prefix + Count)"'
			},
			{
				label: __('Zero-pad Digits (e.g. A-01)'),
				fieldname: 'pad_zeros',
				fieldtype: 'Check',
				default: 1,
				depends_on: 'eval:doc.mode=="Sequential Range (Prefix + Count)"'
			},
			{
				label: __('Stall Numbers List'),
				fieldname: 'custom_numbers',
				fieldtype: 'Small Text',
				depends_on: 'eval:doc.mode=="Custom Stall Numbers List"',
				placeholder: __('Enter comma or newline separated stall numbers, e.g. A-01, A-02, A-03...')
			}
		],
		primary_action_label: __('Create Stalls'),
		primary_action: function(values) {
			if (!values.price || values.price <= 0) {
				frappe.msgprint(__('Price must be greater than 0'));
				return;
			}
			if (values.mode === 'Sequential Range (Prefix + Count)') {
				if (!values.count || values.count <= 0) {
					frappe.msgprint(__('Number of Stalls (Count) must be at least 1'));
					return;
				}
				if (!values.prefix) {
					values.prefix = (values.block ? values.block + '-' : 'A-');
				}
			} else {
				if (!values.custom_numbers || !values.custom_numbers.trim()) {
					frappe.msgprint(__('Please provide at least one stall number'));
					return;
				}
			}
			frappe.call({
				method: 'connectfest.api.admin_bulk_create_stalls',
				args: values,
				freeze: true,
				freeze_message: __('Creating stalls...'),
				callback: function(r) {
					if (r.message && r.message.status === 'success') {
						frappe.show_alert({
							message: r.message.message,
							indicator: 'green'
						}, 5);
						d.hide();
						if (caller && caller.refresh) {
							caller.refresh();
						}
					} else {
						frappe.msgprint(r.message ? r.message.message : __('Failed to create stalls'));
					}
				}
			});
		}
	});

	// Auto-update prefix when block changes
	d.fields_dict.block && d.fields_dict.block.$input && d.fields_dict.block.$input.on('change', function() {
		let blk = d.get_value('block');
		if (blk && (!d.get_value('prefix') || d.get_value('prefix') === 'A-')) {
			d.set_value('prefix', blk.toUpperCase() + '-');
		}
	});

	d.show();
};
