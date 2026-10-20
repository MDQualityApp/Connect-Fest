"""
ConnectFest 2027 - Modular Backend API Gateway
Structured modular services:
- connectfest.services.common: Constants, Redis cache/lock keys, payload parsers
- connectfest.services.pricing: Dynamic admin pricing & tier price calculation
- connectfest.services.auth: Frappe User provisioning, attendee login, and profile
- connectfest.services.stalls: 195 stall inventory, theatre seat locking, admin block
- connectfest.services.registration: Exhibitor & Visitor registration, stall selection
- connectfest.services.payments: Razorpay test/live checkout, preview, finalize
- connectfest.services.passes: Digital QR pass verification & attendee dashboard
- connectfest.services.queue: Redis in-memory ingestion & bulk MariaDB flush
"""

import frappe
from connectfest.services.common import (
	STALL_QUEUE_KEY,
	VISITOR_QUEUE_KEY,
	STALL_CACHE_PREFIX,
	VISITOR_CACHE_PREFIX,
	STALL_COUNTER_KEY,
	VISITOR_COUNTER_KEY,
	STALL_LOCK_PREFIX,
	BATCH_CHUNK_SIZE,
	VALID_SOURCES,
	parse_boolean,
	extract_request_data,
	get_cached_record,
	set_cached_record,
	generate_stall_id,
	generate_visitor_id,
)
from connectfest.services.pricing import (
	get_tier_pricing,
	get_all_tier_pricing as _get_all_tier_pricing,
	DynamicStallPricing,
	STALL_PRICING,
	update_dynamic_pricing as _update_dynamic_pricing,
	admin_create_tier_pricing as _admin_create_tier_pricing,
	admin_update_tier_pricing as _admin_update_tier_pricing,
	admin_delete_tier_pricing as _admin_delete_tier_pricing,
)
from connectfest.services.auth import (
	ensure_frappe_user,
	get_current_user_profile as _get_current_user_profile,
	login_attendee as _login_attendee,
	logout as _logout,
	admin_login as _admin_login,
	admin_toggle_user_status as _admin_toggle_user_status,
	request_contact_update as _request_contact_update,
	verify_and_update_contact as _verify_and_update_contact,
	update_profile as _update_profile,
)
from connectfest.services.stalls import (
	get_stalls as _get_stalls,
	hold_stall as _hold_stall,
	release_stall_hold as _release_stall_hold,
	toggle_stall_block as _toggle_stall_block,
	get_admin_dashboard_data as _get_admin_dashboard_data,
	admin_get_stalls as _admin_get_stalls,
	admin_create_stall as _admin_create_stall,
	admin_get_stall as _admin_get_stall,
	admin_update_stall as _admin_update_stall,
	admin_delete_stall as _admin_delete_stall,
	admin_restore_defaults as _admin_restore_defaults,
)
from connectfest.services.registration import (
	register_stall as _register_stall,
	select_stall as _select_stall,
	register_visitor as _register_visitor,
	check_user_booking_status as _check_user_booking_status,
)
from connectfest.services.payments import (
	get_razorpay_config,
	create_razorpay_order as _create_razorpay_order,
	verify_razorpay_payment as _verify_razorpay_payment,
	preview_payment as _preview_payment,
	checkout as _checkout,
	finalize_payment as _finalize_payment,
)
from connectfest.services.passes import (
	verify_gate_pass as _verify_gate_pass,
	get_registration as _get_registration,
	get_my_dashboard as _get_my_dashboard,
)
from connectfest.services.queue import (
	trigger_async_flush,
	flush_registration_queue,
	get_queue_stats as _get_queue_stats,
	flush_queue as _flush_queue,
)
from connectfest.services.otp import (
	send_otp as _send_otp,
	verify_otp as _verify_otp,
)


# ==============================================================================
# 1. REGISTRATION APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def register_stall(**kwargs):
	"""Exhibitor Registration (Name, Company, Location, BNI, Contact details)."""
	return _register_stall(**kwargs)


@frappe.whitelist(allow_guest=True)
def register(**kwargs):
	"""Default registration alias pointing to register_stall."""
	return _register_stall(**kwargs)


@frappe.whitelist(allow_guest=True)
def select_stall(registration_id=None, stall_number=None, **kwargs):
	"""Select or change stall for a registered exhibitor before payment."""
	return _select_stall(registration_id=registration_id, stall_number=stall_number, **kwargs)


@frappe.whitelist(allow_guest=True)
def register_visitor(**kwargs):
	"""Visitor Free Registration (Name, Email, Mobile -> QR Pass)."""
	return _register_visitor(**kwargs)


@frappe.whitelist(allow_guest=True)
def check_user_booking_status(email=None, mobile_no=None, **kwargs):
	"""Check if an email or mobile has existing stall or visitor booking."""
	return _check_user_booking_status(email=email, mobile_no=mobile_no, **kwargs)


# ==============================================================================
# 2. PAYMENT & NUQID CHECKOUT APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def create_razorpay_order(registration_id=None, stall_number=None, stall_category=None, **kwargs):
	"""Creates Razorpay order. stall_number is STRICTLY MANDATORY at payment."""
	return _create_razorpay_order(registration_id=registration_id, stall_number=stall_number, stall_category=stall_category, **kwargs)


@frappe.whitelist(allow_guest=True)
def preview_payment(**kwargs):
	"""Previews pricing, tier, dimensions for the chosen stall."""
	return _preview_payment(**kwargs)


@frappe.whitelist(allow_guest=True)
def checkout(**kwargs):
	"""Initiates Razorpay checkout popup payload."""
	return _checkout(**kwargs)


@frappe.whitelist(allow_guest=True)
def finalize_payment(**kwargs):
	"""Verifies Razorpay HMAC signature and confirms stall booking."""
	return _finalize_payment(**kwargs)


@frappe.whitelist(allow_guest=True)
def verify_razorpay_payment(**kwargs):
	"""Signature verification core."""
	return _verify_razorpay_payment(**kwargs)


# ==============================================================================
# 3. STALL INVENTORY & THEATRE LOCKING APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def get_stalls(**kwargs):
	"""Returns all 195 stalls with live booking status and zones."""
	return _get_stalls(**kwargs)


@frappe.whitelist(allow_guest=True)
def hold_stall(stall_number=None, **kwargs):
	"""Locks stall in Redis for 10 minutes during checkout."""
	return _hold_stall(stall_number=stall_number, **kwargs)


@frappe.whitelist(allow_guest=True)
def release_stall_hold(stall_number=None, hold_token=None, **kwargs):
	"""Releases temporary stall hold."""
	return _release_stall_hold(stall_number=stall_number, hold_token=hold_token, **kwargs)


@frappe.whitelist(allow_guest=True)
def toggle_stall_block(stall_number=None, action="block", reason=None, **kwargs):
	"""Admin API to block/unblock stalls."""
	return _toggle_stall_block(stall_number=stall_number, action=action, reason=reason, **kwargs)


@frappe.whitelist(allow_guest=True)
def get_admin_dashboard_data(**kwargs):
	"""Real-time dashboard metrics and inventory overview."""
	return _get_admin_dashboard_data(**kwargs)


# ==============================================================================
# 4. PASSES & DASHBOARD APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def verify_gate_pass(pass_code=None, **kwargs):
	"""Gate entry QR scanner validation with duplicate prevention."""
	return _verify_gate_pass(pass_code=pass_code, **kwargs)


@frappe.whitelist(allow_guest=True)
def get_registration(registration_id):
	"""Universal lookup by Registration ID."""
	return _get_registration(registration_id)


@frappe.whitelist(allow_guest=True)
def get_my_dashboard(email=None, mobile_no=None, **kwargs):
	"""Attendee dashboard alias returning complete user profile and bookings."""
	kwargs["email"] = email
	kwargs["mobile_no"] = mobile_no
	return _get_current_user_profile(**kwargs)


@frappe.whitelist(allow_guest=True)
def get_current_user_profile(**kwargs):
	"""Resolves logged-in user profile from Frappe session or token."""
	return _get_current_user_profile(**kwargs)


@frappe.whitelist(allow_guest=True)
def me(**kwargs):
	"""Alias for get_current_user_profile."""
	return _get_current_user_profile(**kwargs)


@frappe.whitelist(allow_guest=True)
def login_attendee(email=None, mobile_no=None, password=None, **kwargs):
	"""Attendee login endpoint by email or mobile number."""
	return _login_attendee(email=email, mobile_no=mobile_no, password=password, **kwargs)


@frappe.whitelist(allow_guest=True)
def send_otp(mobile_no=None, **kwargs):
	"""Generates and sends 6-digit OTP to mobile number (5 min validity)."""
	return _send_otp(mobile_no=mobile_no, **kwargs)


@frappe.whitelist(allow_guest=True)
def verify_otp(mobile_no=None, otp=None, **kwargs):
	"""Verifies OTP, logs attendee into Frappe session, and returns dashboard."""
	return _verify_otp(mobile_no=mobile_no, otp=otp, **kwargs)


@frappe.whitelist(allow_guest=True)
def logout(**kwargs):
	"""Logs out the current session user and clears authentication cookies."""
	return _logout()


@frappe.whitelist(allow_guest=True)
def admin_login(email=None, password=None, otp=None, **kwargs):
	"""Dedicated Admin Login with Two-Factor Authentication (Password + 2FA OTP)."""
	return _admin_login(email=email, password=password, otp=otp, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_toggle_user_status(user_email=None, mobile_no=None, enabled=1, **kwargs):
	"""Admin API to enable or disable attendee User accounts."""
	return _admin_toggle_user_status(user_email=user_email, mobile_no=mobile_no, enabled=enabled, **kwargs)


@frappe.whitelist(allow_guest=True)
def request_contact_update(new_mobile_no=None, new_email=None, **kwargs):
	"""Initiates phone number or email address update with 2-minute OTP verification."""
	return _request_contact_update(new_mobile_no=new_mobile_no, new_email=new_email, **kwargs)


@frappe.whitelist(allow_guest=True)
def verify_and_update_contact(otp=None, **kwargs):
	"""Verifies OTP and updates mobile number and/or email in Frappe User and Registration records."""
	return _verify_and_update_contact(otp=otp, **kwargs)


@frappe.whitelist(allow_guest=True)
def update_profile(**kwargs):
	"""Updates attendee profile details (name, company, logo, location, industry, segment, etc.) for the logged-in user."""
	return _update_profile(**kwargs)


# ==============================================================================
# 5. ADMIN PRICING & QUEUE APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def get_all_tier_pricing(**kwargs):
	"""Returns all active pricing tiers with Base Price, GST %, GST Amount, Total, and Plan Description."""
	return _get_all_tier_pricing(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_create_tier_pricing(tier_name=None, price=None, gst_percent=18.0, description=None, **kwargs):
	"""Admin API: Create a new dynamic pricing tier / plan with GST and description."""
	return _admin_create_tier_pricing(tier_name=tier_name, price=price, gst_percent=gst_percent, description=description, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_update_tier_pricing(tier_name=None, price=None, gst_percent=None, description=None, is_active=None, **kwargs):
	"""Admin API: Update an existing tier's price, GST percentage, description, or active status."""
	return _admin_update_tier_pricing(tier_name=tier_name, price=price, gst_percent=gst_percent, description=description, is_active=is_active, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_delete_tier_pricing(tier_name=None, **kwargs):
	"""Admin API: Delete a pricing tier. Prevented if stalls are already booked!"""
	return _admin_delete_tier_pricing(tier_name=tier_name, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_get_stalls(status=None, tier=None, search=None, page=1, limit=50, **kwargs):
	"""Admin Stall Inventory & Buyer Report API: Status filter, buyer info for booked stalls, and blocked stalls."""
	return _admin_get_stalls(status=status, tier=tier, search=search, page=page, limit=limit, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_create_stall(stall_number=None, tier="Regular", dimensions=None, price=None, zone=None, **kwargs):
	"""Admin API: Create a new exhibition stall."""
	return _admin_create_stall(stall_number=stall_number, tier=tier, dimensions=dimensions, price=price, zone=zone, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_get_stall(stall_number=None, **kwargs):
	"""Admin API: Get specific stall details with buyer info and hold status."""
	return _admin_get_stall(stall_number=stall_number, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_update_stall(stall_number=None, **kwargs):
	"""Admin API: Update stall properties (tier, dimensions, price, status, block/unblock)."""
	return _admin_update_stall(stall_number=stall_number, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_delete_stall(stall_number=None, **kwargs):
	"""Admin API: Delete a stall. PREVENTED if stall is already booked!"""
	return _admin_delete_stall(stall_number=stall_number, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_restore_defaults(reset_pricing=1, reset_stalls=1, reset_booked=0, **kwargs):
	"""Admin API: Explicitly restore pristine default pricing (4 tiers) and default 195 stalls on-demand."""
	return _admin_restore_defaults(reset_pricing=reset_pricing, reset_stalls=reset_stalls, reset_booked=reset_booked, **kwargs)


@frappe.whitelist(allow_guest=True)
def update_dynamic_pricing(silver=None, premium=None, standard=None, regular=None, **kwargs):
	"""Admin dynamic price updates for all tiers."""
	return _update_dynamic_pricing(silver=silver, premium=premium, standard=standard, regular=regular, **kwargs)


@frappe.whitelist(allow_guest=True)
def get_queue_stats():
	"""Queue monitoring stats."""
	return _get_queue_stats()


@frappe.whitelist(allow_guest=True)
def flush_queue():
	"""Manual Redis to MariaDB queue flush."""
	return _flush_queue()
