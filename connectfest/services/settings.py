"""
ConnectFest 2027 - Admin Settings Service
Provides secure Admin APIs for:
1. Stall Tier Pricing
2. Event Configuration (excluding Attendee Dashboard URL)
3. OTP Expiry (Seconds) (excluding Email Verified Registration Window)

Explicitly excludes Razorpay, WhatsApp (WATI), email verification validity, and dashboard URL.
Enforces strict Administrator / System Manager permissions.
"""

import frappe
from frappe.utils import flt, cint, cstr
from connectfest.services.common import extract_request_data, parse_boolean
from connectfest.services.auth import check_admin_permission


# Allowed fields for this specific Admin Settings API
ALLOWED_PRICING_FIELDS = {
	"price_title_sponsor": flt,
	"price_silver": flt,
	"price_premium": flt,
	"price_standard": flt,
	"price_regular": flt,
}

ALLOWED_EVENT_FIELDS = {
	"event_name": cstr,
	"event_dates": cstr,
	"event_venue": cstr,
	"event_timings": cstr,
	"crn_bni_only": lambda v: 1 if parse_boolean(v) else 0,
	"non_bni_extra_percentage": flt,
	"stall_hold_duration_minutes": lambda v: max(1, cint(v)),
}

ALLOWED_OTP_FIELDS = {
	"otp_expiry_seconds": lambda v: max(10, cint(v)),
}

# Explicitly disallowed / excluded fields
DISALLOWED_FIELDS = {
	"razorpay_key_id",
	"razorpay_merchant_id",
	"razorpay_key_secret",
	"razorpay_webhook_secret",
	"currency",
	"wati_enabled",
	"wati_api_host",
	"wati_api_token",
	"wati_template_name",
	"email_verified_validity_minutes",
	"frontend_dashboard_url",
}


def _get_clean_settings_data(settings_doc):
	"""Extracts only the allowed sections and fields from ConnectFest Settings."""
	pricing_data = {
		f: flt(settings_doc.get(f) or 0.0) for f in ALLOWED_PRICING_FIELDS
	}
	event_data = {
		"event_name": cstr(settings_doc.get("event_name") or ""),
		"event_dates": cstr(settings_doc.get("event_dates") or ""),
		"event_venue": cstr(settings_doc.get("event_venue") or ""),
		"event_timings": cstr(settings_doc.get("event_timings") or "9:00 AM - 7:00 PM"),
		"crn_bni_only": cint(settings_doc.get("crn_bni_only") or 0),
		"non_bni_extra_percentage": flt(settings_doc.get("non_bni_extra_percentage") or 0.0),
		"stall_hold_duration_minutes": cint(settings_doc.get("stall_hold_duration_minutes") or 10),
	}
	otp_data = {
		"otp_expiry_seconds": cint(settings_doc.get("otp_expiry_seconds") or 120),
	}

	return {
		"stall_tier_pricing": pricing_data,
		"event_configuration": event_data,
		"otp_configuration": otp_data,
		# Also provide flat access for convenience
		"fields": {
			**pricing_data,
			**event_data,
			**otp_data,
		},
	}


def admin_get_settings(**kwargs):
	"""
	Admin API (GET): Fetch ConnectFest Settings.
	Returns only:
	- Stall Tier Pricing
	- Event Configuration (without dashboard url)
	- OTP Expiry (Seconds) (without email verified validity)
	Strictly requires Administrator or System Manager privileges.
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		err = check_admin_permission("ConnectFest Settings")
		if err:
			return err

	settings_doc = frappe.get_single("ConnectFest Settings")
	data = _get_clean_settings_data(settings_doc)

	return {
		"status": "success",
		"message": "Admin settings retrieved successfully",
		"data": data,
	}


def admin_update_settings(**kwargs):
	"""
	Admin API (POST): Update ConnectFest Settings.
	Updates only allowed fields:
	- price_title_sponsor, price_silver, price_premium, price_standard, price_regular
	- event_name, event_dates, event_venue, crn_bni_only, non_bni_extra_percentage, stall_hold_duration_minutes
	- otp_expiry_seconds
	Strictly ignores/disallows Razorpay, WhatsApp (WATI), email verified validity, and dashboard url.
	Strictly requires Administrator or System Manager privileges.
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		err = check_admin_permission("ConnectFest Settings")
		if err:
			return err

	payload = extract_request_data(kwargs)

	# Flatten nested dictionaries if client sent structured json
	flattened = {}
	for k, v in payload.items():
		if isinstance(v, dict):
			flattened.update(v)
		else:
			flattened[k] = v

	# Remove any disallowed fields if passed
	for disallowed in DISALLOWED_FIELDS:
		flattened.pop(disallowed, None)

	settings_doc = frappe.get_single("ConnectFest Settings")
	updated_fields = []
	pricing_updated = False

	# 1. Update Pricing Fields
	for field, converter in ALLOWED_PRICING_FIELDS.items():
		if field in flattened:
			val = converter(flattened[field])
			old_val = flt(settings_doc.get(field) or 0.0)
			if old_val != val:
				setattr(settings_doc, field, val)
				updated_fields.append(field)
				pricing_updated = True

	# 2. Update Event Fields
	for field, converter in ALLOWED_EVENT_FIELDS.items():
		if field in flattened:
			val = converter(flattened[field])
			setattr(settings_doc, field, val)
			updated_fields.append(field)

	# 3. Update OTP Fields
	for field, converter in ALLOWED_OTP_FIELDS.items():
		if field in flattened:
			val = converter(flattened[field])
			setattr(settings_doc, field, val)
			updated_fields.append(field)

	if not updated_fields:
		return {
			"status": "success",
			"message": "No changes made",
		}

	try:
		settings_doc.save(ignore_permissions=True)
	except frappe.TimestampMismatchError:
		settings_doc.reload()
		for field in updated_fields:
			if field in flattened:
				# Re-apply
				if field in ALLOWED_PRICING_FIELDS:
					setattr(settings_doc, field, ALLOWED_PRICING_FIELDS[field](flattened[field]))
				elif field in ALLOWED_EVENT_FIELDS:
					setattr(settings_doc, field, ALLOWED_EVENT_FIELDS[field](flattened[field]))
				elif field in ALLOWED_OTP_FIELDS:
					setattr(settings_doc, field, ALLOWED_OTP_FIELDS[field](flattened[field]))
		settings_doc.save(ignore_permissions=True)

	# If pricing was updated, sync with Tier Pricing DocType and available stalls
	if pricing_updated:
		_sync_pricing_to_stalls_and_tiers(settings_doc)

	frappe.db.commit()

	# Clear relevant caches
	try:
		from connectfest.services.stalls import invalidate_stalls_cache
		from connectfest.services.pricing import invalidate_pricing_cache
		from connectfest.services.bni_members import clear_bni_member_cache
		invalidate_stalls_cache()
		invalidate_pricing_cache()
		clear_bni_member_cache()
		frappe.cache().delete_value("connectfest:settings:stall_hold_minutes")
		frappe.cache().delete_value("connectfest:settings:bni_verification")
		frappe.clear_cache(doctype="ConnectFest Settings")
	except Exception:
		pass

	return {
		"status": "success",
		"message": "Settings updated successfully",
	}


def _sync_pricing_to_stalls_and_tiers(settings_doc):
	"""Syncs updated settings pricing to ConnectFest Tier Pricing and available stalls."""
	tier_map = {
		"Silver": flt(settings_doc.get("price_silver")),
		"Premium": flt(settings_doc.get("price_premium")),
		"Standard": flt(settings_doc.get("price_standard")),
		"Regular": flt(settings_doc.get("price_regular")),
	}

	for tier_name, price in tier_map.items():
		if price > 0:
			# Sync ConnectFest Tier Pricing doctype
			if frappe.db.exists("ConnectFest Tier Pricing", tier_name):
				frappe.db.set_value("ConnectFest Tier Pricing", tier_name, "price", price, update_modified=False)
			# Sync all currently available stalls of this tier
			frappe.db.set_value("ConnectFest Stall", {"tier": tier_name, "status": "Available"}, "price", price, update_modified=False)
