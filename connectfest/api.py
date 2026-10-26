"""
ConnectFest 2027 - Modular Backend API Gateway
Structured modular services:
- connectfest.services.common: Constants, Redis cache/lock keys, payload parsers
- connectfest.services.pricing: Dynamic admin pricing & tier price calculation
- connectfest.services.auth: Frappe User provisioning, attendee login, and profile
- connectfest.services.stalls: 220 stall inventory, theatre seat locking, admin block
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
	before_request,
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
	admin_get_all_users as _admin_get_all_users,
	admin_get_user_details as _admin_get_user_details,
	admin_delete_user as _admin_delete_user,
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
	admin_book_stall as _admin_book_stall,
	admin_restore_defaults as _admin_restore_defaults,
	admin_clear_all_bookings as _admin_clear_all_bookings,
	setup_default_stalls as _setup_default_stalls,
	seed_default_stalls as _seed_default_stalls,
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
	admin_confirm_payment as _admin_confirm_payment,
)
from connectfest.services.passes import (
	admin_verify_gate_pass as _admin_verify_gate_pass,
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
from connectfest.services.gallery import (
	get_gallery as _get_gallery,
	get_gallery_item as _get_gallery_item,
	admin_create_gallery_item as _admin_create_gallery_item,
	admin_get_gallery_items as _admin_get_gallery_items,
	admin_update_gallery_item as _admin_update_gallery_item,
	admin_delete_gallery_item as _admin_delete_gallery_item,
	admin_toggle_gallery_pin as _admin_toggle_gallery_pin,
	admin_upload_gallery_image as _admin_upload_gallery_image,
)
from connectfest.services.sponsors import (
	get_sponsors as _get_sponsors,
	get_sponsor_details as _get_sponsor_details,
	admin_create_sponsor as _admin_create_sponsor,
	admin_get_sponsors as _admin_get_sponsors,
	admin_update_sponsor as _admin_update_sponsor,
	admin_delete_sponsor as _admin_delete_sponsor,
	admin_toggle_sponsor_status as _admin_toggle_sponsor_status,
	setup_default_sponsors as _setup_default_sponsors,
	seed_default_sponsors as _seed_default_sponsors,
)
from connectfest.services.notifications import (
	send_booking_confirmation_email as _send_booking_confirmation_email,
	send_payment_failure_email as _send_payment_failure_email,
	resend_booking_email as _resend_booking_email,
)
from connectfest.services.reports import (
	admin_export_bookings_csv as _admin_export_bookings_csv,
	admin_export_bookings_pdf as _admin_export_bookings_pdf,
	admin_get_bookings_report as _admin_get_bookings_report,
	admin_export as _admin_export,
	download_stall_allocation_letter as _download_stall_allocation_letter,
)
from connectfest.services.terms import (
	get_terms_and_conditions as _get_terms_and_conditions,
	get_terms_by_type as _get_terms_by_type,
	admin_get_terms_and_conditions as _admin_get_terms_and_conditions,
	admin_get_terms_item as _admin_get_terms_item,
	admin_create_terms_and_conditions as _admin_create_terms_and_conditions,
	admin_update_terms_and_conditions as _admin_update_terms_and_conditions,
	admin_delete_terms_and_conditions as _admin_delete_terms_and_conditions,
	setup_default_terms as _setup_default_terms,
	seed_default_terms as _seed_default_terms,
)



# ==============================================================================
# 1. REGISTRATION APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def register_stall(**kwargs):
	"""Exhibitor Registration (Name, Company, Location, Bill, GST, Address, BNI, Contact details)."""
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
	"""Visitor Free Registration (Name, Email, Mobile, Area of Interest -> QR Pass)."""
	return _register_visitor(**kwargs)


@frappe.whitelist(allow_guest=True)
def check_user_booking_status(email=None, mobile_no=None, **kwargs):
	"""Check if an email or mobile has existing stall or visitor booking."""
	return _check_user_booking_status(email=email, mobile_no=mobile_no, **kwargs)


@frappe.whitelist(allow_guest=True)
def cleanup_loadtest_data(secret_key=None, **kwargs):
	"""
	Secure cleanup endpoint for load test dummy data (@loadtest.connectfest.local and STALL-TEST-%).
	Can be triggered locally or via live HTTP request with secret_key.
	"""
	from connectfest.services.auth import is_admin_user
	from connectfest.services.common import extract_request_data
	data = extract_request_data(kwargs)
	key = (secret_key or data.get("secret_key") or "").strip()
	sess_user = getattr(frappe.session, "user", None)
	valid_key = "cf_loadtest_cleanup_2027"

	if not is_admin_user(sess_user) and key != valid_key:
		frappe.local.response["http_status_code"] = 403
		return {"status": "forbidden", "message": "Admin authorization or valid secret_key required."}

	pattern = "%@loadtest.connectfest.local"
	stall_pattern = "STALL-TEST-%"

	frappe.db.sql("DELETE FROM `tabConnectFest Registration` WHERE email LIKE %s", (pattern,))
	frappe.db.sql("DELETE FROM `tabConnectFest Visitor Registration` WHERE email LIKE %s", (pattern,))

	users = frappe.db.sql("SELECT email FROM `tabUser` WHERE email LIKE %s", (pattern,), as_dict=True)
	if users:
		u_emails = [u["email"] for u in users]
		format_strings = ','.join(['%s'] * len(u_emails))
		frappe.db.sql(f"DELETE FROM `tabHas Role` WHERE parent IN ({format_strings})", tuple(u_emails))
	frappe.db.sql("DELETE FROM `tabUser` WHERE email LIKE %s", (pattern,))
	frappe.db.sql("DELETE FROM `tabConnectFest Stall` WHERE stall_number LIKE %s", (stall_pattern,))

	frappe.db.commit()
	return {
		"status": "success",
		"message": "All test registrations, test users, and test stalls cleaned successfully without disturbing existing data!"
	}


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
	"""Signature verification core for attendee online payments."""
	return _verify_razorpay_payment(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_confirm_payment(**kwargs):
	"""Admin API: Confirm payment directly (Cash or Online) and allocate stall for exhibitor."""
	return _admin_confirm_payment(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_record_cash_payment(**kwargs):
	"""Alias for admin_confirm_payment with default Cash mode."""
	kwargs["payment_mode"] = kwargs.get("payment_mode") or "Cash"
	return _admin_confirm_payment(**kwargs)


# ==============================================================================
# 3. STALL INVENTORY & THEATRE LOCKING APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def get_stalls(**kwargs):
	"""Returns all 220 stalls with live booking status, blueprint coordinates, and zones."""
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
def toggle_stall_block(stall_number=None, action=None, reason=None, force=0, **kwargs):
	"""Admin API to block/unblock stalls."""
	return _toggle_stall_block(stall_number=stall_number, action=action, reason=reason, force=force, **kwargs)


@frappe.whitelist(allow_guest=True)
def get_admin_dashboard_data(**kwargs):
	"""Real-time dashboard metrics and inventory overview."""
	return _get_admin_dashboard_data(**kwargs)


@frappe.whitelist(allow_guest=True)
def get_live_stall_updates(limit=50, since=None, **kwargs):
	"""Real-time stall updates and active holds for live frontend map synchronization."""
	from connectfest.services.realtime import get_live_stall_updates as _get_live_stall_updates
	return _get_live_stall_updates(limit=limit, since=since)


# ==============================================================================
# 4. PASSES & DASHBOARD APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def admin_verify_gate_pass(pass_code=None, day=None, **kwargs):
	"""Admin Gate Entry QR scanner validation with day verification and duplicate prevention."""
	return _admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_verify_gate_passs(pass_code=None, day=None, **kwargs):
	"""Typo-tolerant alias for admin_verify_gate_pass."""
	return _admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


@frappe.whitelist(allow_guest=True)
def verify_gate_pass(pass_code=None, day=None, **kwargs):
	"""Gate entry QR scanner validation alias pointing to admin_verify_gate_pass."""
	return _admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_verify_pass(pass_code=None, day=None, **kwargs):
	"""Admin Verify Pass alias."""
	return _admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


@frappe.whitelist(allow_guest=True)
def verify_pass(pass_code=None, day=None, **kwargs):
	"""Verify Pass alias."""
	return _admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


@frappe.whitelist(allow_guest=True)
def Verify_Pass(pass_code=None, day=None, **kwargs):
	"""Verify_Pass alias matching Hoppscotch collection."""
	return _admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


@frappe.whitelist(allow_guest=True)
def get_registration(registration_id):
	"""Universal lookup by Registration ID."""
	return _get_registration(registration_id)


@frappe.whitelist(allow_guest=True)
def get_my_dashboard(email=None, mobile_no=None, **kwargs):
	"""Attendee dashboard alias returning complete user profile and bookings."""
	return _get_my_dashboard(email=email, mobile_no=mobile_no, **kwargs)


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
def admin_toggle_user_status(user_email=None, mobile_no=None, enabled=None, **kwargs):
	"""Admin API to enable or disable attendee User accounts."""
	return _admin_toggle_user_status(user_email=user_email, mobile_no=mobile_no, enabled=enabled, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_get_all_users(**kwargs):
	"""Admin API: Returns a unified directory of all attendees, exhibitors, visitors, and admins."""
	return _admin_get_all_users(**kwargs)


@frappe.whitelist(allow_guest=True)
def get_all_users(**kwargs):
	"""Alias for admin_get_all_users."""
	return _admin_get_all_users(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_get_user_details(user_email=None, mobile_no=None, **kwargs):
	"""Admin API: Returns complete details of a specific user by email or mobile number."""
	return _admin_get_user_details(user_email=user_email, mobile_no=mobile_no, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_delete_user(**kwargs):
	"""Admin API: Cascade-deletes user account and releases all their registrations and booked stalls."""
	return _admin_delete_user(**kwargs)


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
def admin_book_stall(**kwargs):
	"""Admin API: Book one or multiple stalls directly for an attendee with Online or Cash payment."""
	return _admin_book_stall(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_book_stall_for_user(**kwargs):
	"""Alias for admin_book_stall."""
	return _admin_book_stall(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_restore_defaults(reset_pricing=1, reset_stalls=1, reset_booked=0, **kwargs):
	"""Admin API: Explicitly restore pristine default pricing (5 tiers) and default 220 stalls on-demand."""
	return _admin_restore_defaults(reset_pricing=reset_pricing, reset_stalls=reset_stalls, reset_booked=reset_booked, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_clear_all_bookings(**kwargs):
	"""
	Admin API: Complete wipe and reset for testing.
	Deletes all Exhibitor Registrations, Visitor Registrations, resets 220 stalls to Available, and clears Redis queues.
	"""
	return _admin_clear_all_bookings(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_reset_test_data(**kwargs):
	"""Alias for admin_clear_all_bookings."""
	return _admin_clear_all_bookings(**kwargs)


@frappe.whitelist(allow_guest=True)
def setup_stalls():
	"""Setup API: Populates or verifies 220 stalls and pricing tiers while preserving bookings."""
	return _setup_default_stalls(preserve_booked=True)

seed_stalls = setup_stalls


@frappe.whitelist(allow_guest=True)
def update_dynamic_pricing(silver=None, premium=None, standard=None, regular=None, title_sponsor=None, sponsor=None, **kwargs):
	"""Admin dynamic price updates for all 5 tiers."""
	return _update_dynamic_pricing(silver=silver, premium=premium, standard=standard, regular=regular, title_sponsor=title_sponsor, sponsor=sponsor, **kwargs)


@frappe.whitelist(allow_guest=True)
def get_queue_stats():
	"""Queue monitoring stats."""
	return _get_queue_stats()


@frappe.whitelist(allow_guest=True)
def flush_queue():
	"""Manual Redis to MariaDB queue flush."""
	return _flush_queue()


# ==============================================================================
# 9. GALLERY APIS (Public Website & Admin Multiple Images CRUD + Pinning)
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def get_gallery(**kwargs):
	"""Public API: Returns published gallery items with multiple images (Pinned items first)."""
	return _get_gallery(**kwargs)


@frappe.whitelist(allow_guest=True)
def get_gallery_item(gallery_id=None, **kwargs):
	"""Public API: Returns a single gallery item with all its images."""
	return _get_gallery_item(gallery_id=gallery_id, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_create_gallery_item(**kwargs):
	"""Admin API: Create a gallery item with multiple images and optional pinning."""
	return _admin_create_gallery_item(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_get_gallery_items(**kwargs):
	"""Admin API: List all gallery items (published + draft) with pagination and filters."""
	return _admin_get_gallery_items(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_update_gallery_item(**kwargs):
	"""Admin API: Update a gallery item (title, description, pin status, images)."""
	return _admin_update_gallery_item(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_delete_gallery_item(gallery_id=None, gallery_ids=None, **kwargs):
	"""Admin API: Delete single or multiple gallery items and their images."""
	return _admin_delete_gallery_item(gallery_id=gallery_id, gallery_ids=gallery_ids, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_delete_gallery_items(gallery_ids=None, **kwargs):
	"""Alias for admin_delete_gallery_item supporting bulk deletion."""
	return _admin_delete_gallery_item(gallery_ids=gallery_ids, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_toggle_gallery_pin(gallery_id=None, gallery_ids=None, is_pinned=None, **kwargs):
	"""Admin API: Pin or unpin single or multiple gallery items to highlight at the top of the website."""
	return _admin_toggle_gallery_pin(gallery_id=gallery_id, gallery_ids=gallery_ids, is_pinned=is_pinned, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_pin_gallery_items(gallery_ids=None, is_pinned=None, **kwargs):
	"""Alias for admin_toggle_gallery_pin supporting bulk pinning."""
	return _admin_toggle_gallery_pin(gallery_ids=gallery_ids, is_pinned=is_pinned, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_upload_gallery_image(**kwargs):
	"""Admin API: Upload an image file or base64 data to get a public /files/... URL."""
	return _admin_upload_gallery_image(**kwargs)


# ==============================================================================
# 10. SPONSOR APIS (Public Website Directory & Admin CRUD + Tiers)
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def get_sponsors(**kwargs):
	"""Public API: Returns active sponsors grouped by category/tier, showing what they sponsored."""
	return _get_sponsors(**kwargs)


@frappe.whitelist(allow_guest=True)
def get_sponsor_details(sponsor_id=None, **kwargs):
	"""Public API: Returns single sponsor full profile."""
	return _get_sponsor_details(sponsor_id=sponsor_id, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_create_sponsor(**kwargs):
	"""Admin API: Add a new sponsor and what they sponsored for."""
	return _admin_create_sponsor(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_get_sponsors(**kwargs):
	"""Admin API: List all sponsors (active and inactive) with summary counts."""
	return _admin_get_sponsors(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_update_sponsor(sponsor_id=None, **kwargs):
	"""Admin API: Update sponsor information."""
	return _admin_update_sponsor(sponsor_id=sponsor_id, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_delete_sponsor(sponsor_id=None, sponsor_ids=None, **kwargs):
	"""Admin API: Delete single or multiple sponsors."""
	return _admin_delete_sponsor(sponsor_id=sponsor_id, sponsor_ids=sponsor_ids, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_delete_sponsors(sponsor_ids=None, **kwargs):
	"""Alias for admin_delete_sponsor supporting bulk deletion."""
	return _admin_delete_sponsor(sponsor_ids=sponsor_ids, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_toggle_sponsor_status(sponsor_id=None, sponsor_ids=None, **kwargs):
	"""Admin API: Toggle active or featured status for sponsors."""
	return _admin_toggle_sponsor_status(sponsor_id=sponsor_id, sponsor_ids=sponsor_ids, **kwargs)


@frappe.whitelist(allow_guest=True)
def setup_default_sponsors():
	"""Sets up default realistic sponsors for ConnectFest 2027."""
	return _setup_default_sponsors()

seed_default_sponsors = setup_default_sponsors


# ==============================================================================
# 11. NOTIFICATIONS & EMAIL ALERTS APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def resend_booking_email(registration_id=None, email=None, **kwargs):
	"""Public / Attendee API: Resend booking confirmation email with pass & stall details."""
	return _resend_booking_email(registration_id=registration_id, email=email, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_resend_booking_email(registration_id=None, email=None, **kwargs):
	"""Admin alias to resend booking confirmation email."""
	return _resend_booking_email(registration_id=registration_id, email=email, **kwargs)


# ==============================================================================
# 12. EXPORT & REPORTING APIS (CSV & PDF DOWNLOADS WITH FILTERS)
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def admin_export_bookings_csv(**kwargs):
	"""
	Admin API: Export attendees and stall bookings as CSV file.
	Supports filters: role (all/exhibitor/visitor), status, payment_status, payment_mode (Online/Cash),
	is_bni_member (1/0), tier (Platinum/Diamond/Gold/Silver), search query.
	"""
	return _admin_export_bookings_csv(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_export_bookings_pdf(**kwargs):
	"""
	Admin API: Export attendees and stall bookings as a beautifully styled PDF report.
	Includes event header, summary KPI cards, detailed records table, and timestamp.
	"""
	return _admin_export_bookings_pdf(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_get_bookings_report(**kwargs):
	"""
	Admin API: Get JSON reporting data with filters and summary analytics for dashboard display.
	"""
	return _admin_get_bookings_report(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_export(**kwargs):
	"""
	Unified Admin Export API:
	Supports format: 'csv', 'pdf', 'json'
	Supports download: 1 or 0
	Supports filters: role, payment_status, payment_mode, is_bni_member, tier, status, search
	"""
	return _admin_export(**kwargs)


@frappe.whitelist(allow_guest=True)
def download_stall_allocation_letter(registration_id=None, mobile_no=None, email=None, **kwargs):
	"""
	Downloads official 3-page Stall Allocation Letter & Sales Invoice (PDF)
	in the official ConnectFest 2027 template format.
	"""
	return _download_stall_allocation_letter(registration_id=registration_id, mobile_no=mobile_no, email=email, **kwargs)


@frappe.whitelist(allow_guest=True)
def download_sales_invoice(registration_id=None, mobile_no=None, email=None, **kwargs):
	"""Alias for download_stall_allocation_letter."""
	return _download_stall_allocation_letter(registration_id=registration_id, mobile_no=mobile_no, email=email, **kwargs)


@frappe.whitelist(allow_guest=True)
def download_invoice(registration_id=None, mobile_no=None, email=None, **kwargs):
	"""Alias for download_stall_allocation_letter."""
	return _download_stall_allocation_letter(registration_id=registration_id, mobile_no=mobile_no, email=email, **kwargs)



# ==============================================================================
# 13. TERMS AND CONDITIONS CRUD APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def get_terms_and_conditions(**kwargs):
	"""Public API: Get active terms and conditions, optionally filtered by type or version."""
	return _get_terms_and_conditions(**kwargs)


@frappe.whitelist(allow_guest=True)
def get_terms_by_type(terms_type=None, **kwargs):
	"""Public API: Get latest active terms for a specific type (e.g. Exhibitor or Visitor)."""
	return _get_terms_by_type(terms_type=terms_type, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_get_terms_and_conditions(**kwargs):
	"""Admin API: List all terms and conditions (active & inactive) with filters and breakdown."""
	return _admin_get_terms_and_conditions(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_get_terms_item(terms_id=None, **kwargs):
	"""Admin API: Get a single terms and conditions document by ID."""
	return _admin_get_terms_item(terms_id=terms_id, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_create_terms_and_conditions(**kwargs):
	"""Admin API: Create a new terms and conditions document."""
	return _admin_create_terms_and_conditions(**kwargs)


@frappe.whitelist(allow_guest=True)
def admin_update_terms_and_conditions(terms_id=None, **kwargs):
	"""Admin API: Update an existing terms and conditions document."""
	return _admin_update_terms_and_conditions(terms_id=terms_id, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_delete_terms_and_conditions(terms_id=None, terms_ids=None, **kwargs):
	"""Admin API: Delete one or multiple terms and conditions documents."""
	return _admin_delete_terms_and_conditions(terms_id=terms_id, terms_ids=terms_ids, **kwargs)


@frappe.whitelist(allow_guest=True)
def admin_delete_terms(terms_ids=None, **kwargs):
	"""Alias for admin_delete_terms_and_conditions supporting bulk deletion."""
	return _admin_delete_terms_and_conditions(terms_ids=terms_ids, **kwargs)



@frappe.whitelist(allow_guest=True)
def setup_default_terms():
	"""Sets up default realistic terms and conditions for ConnectFest 2027."""
	return _setup_default_terms()

seed_default_terms = setup_default_terms

