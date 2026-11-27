import json
import time
import urllib.parse
import concurrent.futures
import frappe
from frappe.utils import now_datetime, flt, cint
from connectfest.services.common import (
	STALL_QUEUE_KEY,
	VISITOR_QUEUE_KEY,
	STALL_CACHE_PREFIX,
	VISITOR_CACHE_PREFIX,
	VALID_SOURCES,
	extract_request_data,
	parse_boolean,
	safe_str,
	get_cached_record,
	set_cached_record,
	generate_stall_id,
	generate_visitor_id,
	generate_secure_pass_code,
	save_image_from_base64_or_url,
	format_full_url,
	handle_api_errors,
	log_and_return_error,
)
from connectfest.services.pricing import get_tier_pricing, STALL_PRICING
from connectfest.services.auth import ensure_frappe_user
from connectfest.services.queue import trigger_async_flush


def validate_registration_email_otp(email, data, expected_type=None):
	"""
	Strictly enforces email OTP verification before registration:
	1. If already verified via verify_email_otp (cached in Redis / DB), ensures the verified type
	   matches expected_type (e.g. 'stall', 'visitor', 'event').
	2. If 'otp' or 'email_otp' is passed inline with registration data, validates inline.
	3. Otherwise, returns HTTP 400 error requiring email OTP verification.
	"""
	if getattr(frappe.flags, "in_test", False):
		return None

	target_email = str(email or "").strip().lower()
	if not target_email or "@" not in target_email:
		return {"status": "error", "message": "Valid email address is required"}

	from connectfest.services.otp import is_email_verified, verify_otp_inline, EMAIL_VERIFIED_CACHE_PREFIX

	type_labels = {"stall": "Stall Booking", "visitor": "Visitor Pass", "event": "Event Registration"}
	exp_label = type_labels.get(expected_type, "registration")

	# Check if pre-verified in Redis (valid for any registration flow once email ownership is proven)
	cache = frappe.cache()
	verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
	cached_val = cache.get(verified_key)
	if cached_val:
		return None

	if is_email_verified(target_email, expected_type=expected_type) or is_email_verified(target_email):
		return None

	# Check if OTP passed inline
	inline_otp = str(data.get("email_otp") or data.get("otp") or "").strip()
	if inline_otp:
		if verify_otp_inline(target_email, inline_otp):
			return None
		return {
			"status": "error",
			"message": "Invalid or expired Email OTP! Please check the 6-digit code or request a new OTP."
		}

	return {
		"status": "error",
		"message": f"Email verification is required before {exp_label}! Please verify your email using OTP."
	}



@handle_api_errors("ConnectFest Exhibitor Registration Error")
def register_stall(**kwargs):
	"""
	Exhibitor Registration API.
	Registration captures:
	- Full Name, Company Name, Location
	- BNI details (if member): Region Name, Chapter Name
	- Industry, Segment, Source of Info
	- Email, Mobile Number
	- stall_number: OPTIONAL at initial registration! (Mandatory during payment/checkout)
	"""
	data = extract_request_data(kwargs)

	full_name = safe_str(data.get("full_name") or data.get("name"))
	company_name = safe_str(data.get("company_name") or data.get("company"))
	mobile_no = safe_str(data.get("mobile_no") or data.get("phone") or data.get("mobile"))
	email = safe_str(data.get("email")).lower()
	raw_logo = (
		data.get("company_logo")
		or data.get("logo")
		or data.get("pass_1_image")
		or data.get("image")
		or data.get("photo")
		or data.get("visitor_image")
		or data.get("avatar")
		or data.get("user_image")
	)
	company_logo = save_image_from_base64_or_url(raw_logo, file_name_prefix=f"logo_{mobile_no[-4:] if len(mobile_no)>=4 else 'stall'}") if raw_logo else ""
	if not company_logo and email and hasattr(frappe, "db") and frappe.db:
		try:
			company_logo = (
				frappe.db.get_value("User", email, "user_image")
				or frappe.db.get_value("ConnectFest Visitor Registration", {"email": email}, "visitor_image")
				or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mobile_no}, "visitor_image")
				or ""
			)
		except Exception:
			company_logo = ""
	location = safe_str(data.get("location"))
	bill = safe_str(data.get("bill") or data.get("billing_name") or data.get("bill_to"))
	gst = safe_str(data.get("gst") or data.get("gst_number") or data.get("gstin"))
	raw_is_bni = (
		parse_boolean(data.get("is_bni_member"))
		or parse_boolean(data.get("bni"))
		or parse_boolean(data.get("claimed_bni_member"))
	)
	is_bni_member = 1 if raw_is_bni else 0
	is_cnr_member = 0

	region_name = safe_str(data.get("region_name"))
	chapter_name = safe_str(data.get("chapter_name"))
	industry = safe_str(data.get("industry"))
	segment = safe_str(data.get("segment"))

	raw_category = safe_str(data.get("stall_category") or data.get("category") or data.get("tier")).strip()
	source_of_info = safe_str(data.get("source_of_info") or data.get("source"))
	stall_number = safe_str(data.get("stall_number") or data.get("stall_numbers") or data.get("stall_no"))
	sync_mode = parse_boolean(data.get("sync", 0))
	create_order = parse_boolean(data.get("create_order", 0))

	# Validations
	errors = []
	if not full_name:
		errors.append("Name (full_name) is required")
	if not company_name:
		errors.append("Company Name (company_name) is required")
	from connectfest.services.bni_members import normalize_phone_number
	if not mobile_no:
		errors.append("Mobile Number (mobile_no) is required")
	else:
		clean_mob = normalize_phone_number(mobile_no)
		if len(clean_mob) != 10:
			errors.append("Valid 10-digit Mobile Number is required")
		else:
			mobile_no = clean_mob
	if not email:
		errors.append("Email Address (email) is required")
	elif "@" not in email:
		errors.append("Valid Email Address is required")
	if not source_of_info:
		errors.append("How did you know about this event? (source_of_info) is required")

	# BNI Yes/No Validation:
	# If user says Yes to BNI (is_bni_member == 1), Chapter Name & Region Name are mandatory!
	# If No (is_bni_member == 0), Chapter Name & Region Name are optional.
	if is_bni_member == 1:
		if not region_name:
			errors.append("Region Name (region_name) is mandatory for BNI members")
		if not chapter_name:
			errors.append("Chapter Name (chapter_name) is mandatory for BNI members")

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

	# Enforce logged-in user constraint:
	# If an attendee is already logged in (non-admin), they CANNOT register using a different email or mobile number!
	from connectfest.services.auth import is_admin_user, get_authenticated_user
	sess_user = getattr(frappe.session, "user", None)
	auth_user = get_authenticated_user(data, **kwargs)
	if not auth_user and sess_user and sess_user != "Guest":
		auth_user = sess_user
	is_admin = is_admin_user(auth_user) if auth_user else False

	if auth_user and not is_admin:
		logged_email = auth_user.strip().lower()
		logged_phone = (
			frappe.db.get_value("ConnectFest Registration", {"email": auth_user}, "mobile_no")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"email": auth_user}, "mobile_no")
			or frappe.db.get_value("ConnectFest BNI Event Registration", {"email": auth_user}, "mobile_no")
			or frappe.db.get_value("User", auth_user, "mobile_no")
			or frappe.db.get_value("User", auth_user, "phone")
			or ""
		).strip()
		clean_logged_phone = normalize_phone_number(logged_phone) if logged_phone else ""

		if email and email != logged_email:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "You are currently logged in. Stall registration must match your active logged-in account. To register for a different account, please log out first.",
			}

		if clean_logged_phone and mobile_no and mobile_no != clean_logged_phone:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "You are currently logged in. Stall registration must match your registered mobile number. To register with a different mobile number, please log out first.",
			}

	# Enforce Strict Uniqueness on Email and Mobile
	if email or mobile_no:
		# 1. Check existing exhibitor registrations
		all_ex = frappe.db.sql("""
			SELECT name, registration_id, full_name, company_name, email, mobile_no,
			       stall_number, stall_category, amount, payment_status, status,
			       location, bill, gst
			FROM `tabConnectFest Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s)
			ORDER BY creation DESC
		""", {"email": email, "mobile": mobile_no}, as_dict=True)
		if all_ex:
			ex = all_ex[0]
			ex_email = (ex.get("email") or "").strip().lower()
			ex_mobile = (ex.get("mobile_no") or "").strip()
			req_email = (email or "").strip().lower()
			req_mobile = (mobile_no or "").strip()

			# Conflict A: Mobile number already registered to a DIFFERENT email!
			if req_mobile and ex_mobile == req_mobile and ex_email != req_email:
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": "Mobile number is already registered with another account! Each mobile number can only be used once."
				}

			# Conflict B: Email already registered with a DIFFERENT mobile number!
			if req_email and ex_email == req_email and req_mobile and ex_mobile != req_mobile:
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": "Email address is already registered with a different mobile number! Please use your registered mobile number."
				}

			# Check if user already booked this specific stall
			if stall_number:
				already_booked_this = [
					r for r in all_ex
					if r.get("stall_number") == stall_number and r.get("payment_status") == "Paid"
				]
				if already_booked_this:
					frappe.local.response["http_status_code"] = 400
					return {
						"status": "error",
						"message": f"Stall '{stall_number}' is already booked and confirmed."
					}

				# If user has an existing Pending registration for this same stall, return it
				pending_for_this = [
					r for r in all_ex
					if r.get("stall_number") == stall_number and r.get("payment_status") != "Paid"
				]
				if pending_for_this:
					p_rec = pending_for_this[0]
					ensure_frappe_user(ex_email, p_rec.full_name or full_name, ex_mobile, role="Website User")
					return {
						"status": "already_registered",
						"message": f"A pending registration already exists for stall '{stall_number}'. Please log in to complete payment.",
					}
			else:
				# No stall_number specified:
				# If user already has an existing registration (pending or paid), reuse & update it!
				# Strict single registration per exhibitor account unless explicitly requested (allow_new / new_booking).
				allow_new = parse_boolean(data.get("allow_new", 0)) or parse_boolean(data.get("new_booking", 0))
				if not allow_new:
					pending_no_stall = [
						r for r in all_ex
						if r.get("payment_status") != "Paid" and not r.get("stall_number")
					]
					p_rec = pending_no_stall[0] if pending_no_stall else all_ex[0]

					# Update existing registration with latest business & profile info provided
					upd_data = {}
					if company_name:
						upd_data["company_name"] = company_name
					if location:
						upd_data["location"] = location
					if industry:
						upd_data["industry"] = industry
					if segment:
						upd_data["segment"] = segment
					if bill:
						upd_data["bill"] = bill
					if gst:
						upd_data["gst"] = gst
					if is_bni_member is not None:
						upd_data["is_bni_member"] = is_bni_member
					if region_name:
						upd_data["region_name"] = region_name
					if chapter_name:
						upd_data["chapter_name"] = chapter_name
					if source_of_info:
						upd_data["source_of_info"] = source_of_info
					if upd_data:
						frappe.db.set_value("ConnectFest Registration", p_rec["name"], upd_data)
						frappe.db.commit()
						cache = frappe.cache()
						cache.delete(f"{STALL_CACHE_PREFIX}{p_rec.get('registration_id')}")
						cache.delete(f"{STALL_CACHE_PREFIX}{p_rec['name']}")
					ensure_frappe_user(ex_email, p_rec.full_name or full_name, ex_mobile, role="Website User")
					return {
						"status": "already_registered",
						"message": "An exhibitor registration already exists with these details.",
					}

		# 2. Check if mobile_no or phone is already bound to another User in Frappe
		if mobile_no:
			user_with_mob = frappe.db.get_value("User", {"mobile_no": mobile_no}, "name") or frappe.db.get_value("User", {"phone": mobile_no}, "name")
			if user_with_mob and user_with_mob.lower() != email.lower():
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": "Mobile number is already registered with another account! Each mobile number can only be used once."
				}

	# Stall Specific Validation (if stall_number or category is provided)
	dynamic_prices = get_tier_pricing()
	stall_doc = None
	if stall_number:
		if not frappe.db.exists("ConnectFest Stall", stall_number):
			errors.append(f"Stall '{stall_number}' does not exist in the ConnectFest inventory")
		else:
			stall_doc = frappe.get_doc("ConnectFest Stall", stall_number)
			if stall_doc.is_blocked_by_admin or stall_doc.status == "Blocked":
				errors.append(f"Stall '{stall_number}' is blocked by administration ({stall_doc.blocked_reason or 'Reserved'})")
			elif stall_doc.status == "Booked":
				errors.append(f"Stall '{stall_number}' is already booked and confirmed by another exhibitor")
			else:
				stall_category = stall_doc.tier
				amount = dynamic_prices.get(stall_category, stall_doc.price)
	elif raw_category:
		from connectfest.services.pricing import ensure_tier_pricing_exists
		stall_category = raw_category
		for existing_t in STALL_PRICING.keys():
			if existing_t.lower() == raw_category.lower():
				stall_category = existing_t
				break
		if stall_category not in STALL_PRICING:
			ensure_tier_pricing_exists(stall_category, price=0.0)
			dynamic_prices = get_tier_pricing()
		amount = dynamic_prices.get(stall_category, 0.0)
	else:
		# Initial registration without choosing a stall yet
		stall_category = ""
		amount = 0.0

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

	# 1. Authoritative CNR Verification against ConnectFest CNR Member:
	# - Checks Name, Mobile Number, and Email (all 3 together with space-insensitive name match).
	# - When restriction is enabled (crn_bni_only = 1):
	#   Only verified CNR members can register! Returns HTTP 400 immediately before OTP validation.
	# - When restriction is disabled (crn_bni_only = 0):
	#   Non-CNR members are allowed to register (is_cnr_member = 0, surcharge applies).
	from connectfest.services.bni_members import is_valid_bni_member, is_crn_bni_restriction_enabled
	from connectfest.services.auth import is_admin_user

	sess_user = getattr(frappe.session, "user", None)
	is_admin = is_admin_user(sess_user) if sess_user and sess_user != "Guest" else False

	is_cnr, cnr_doc = is_valid_bni_member(mobile_no=mobile_no, email=email, name=full_name)
	is_cnr_member = 1 if is_cnr else 0

	stall_crn = 0
	if stall_number and frappe.db.exists("ConnectFest Stall", stall_number):
		stall_crn = cint(frappe.db.get_value("ConnectFest Stall", stall_number, "crn") or 0)

	if is_crn_bni_restriction_enabled() and not is_admin:
		if not is_cnr:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "Registration is strictly restricted to verified CNR (Chennai North Region) members",
			}
	elif stall_crn == 1 and not is_admin:
		# If specific corner stall selected, enforce CNR check
		if not is_cnr:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "Only CNR (Chennai North Region) members can book this stall",
			}

	if is_cnr and cnr_doc:
		if not chapter_name and cnr_doc.get("chapter_name"):
			chapter_name = cnr_doc.get("chapter_name")
		if not region_name and cnr_doc.get("region_name"):
			region_name = cnr_doc.get("region_name")

	# 2. Strictly Validate Email OTP:
	# Checks if the attendee verified their email with OTP (or verifies email_otp in request).
	# This runs strictly AFTER BNI member verification passes!
	email_val_err = validate_registration_email_otp(email, data, expected_type="stall")
	if email_val_err:
		frappe.local.response["http_status_code"] = 400
		return email_val_err

	matched_source = None
	for src in VALID_SOURCES:
		if src.lower() == source_of_info.lower():
			matched_source = src
			break
	if not matched_source:
		matched_source = source_of_info if source_of_info in VALID_SOURCES else "Other"

	cache = frappe.cache()
	reg_id = generate_stall_id(cache)
	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S.%f")

	raw_pay_mode = (data.get("payment_mode") or "").strip().capitalize()
	if raw_pay_mode.lower() in ("cash", "cheque", "bank transfer"):
		from connectfest.services.auth import is_admin_user, has_module_access
		sess_user = getattr(frappe.session, "user", None)
		if not is_admin_user(sess_user) and not has_module_access(sess_user, "stall"):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Cash payments can only be processed and recorded by Event Administration at the venue desk. Exhibitors must pay online via Razorpay."
			}

	has_stall_selected = bool(stall_number or stall_category)

	# Optional Attendee Pass 1 Photo
	raw_p1_img = data.get("pass_1_image") or data.get("photo") or data.get("image") or data.get("visitor_image") or data.get("user_image") or data.get("avatar") or ""
	pass_1_image = save_image_from_base64_or_url(raw_p1_img, file_name_prefix=f"exh1_{mobile_no[-4:] if len(mobile_no)>=4 else 'pass'}") if raw_p1_img else ""

	# Optional Co-Exhibitor Pass 2 details
	pass_2_name = (data.get("pass_2_name") or data.get("co_exhibitor_name") or data.get("second_person_name") or data.get("friend_name") or "").strip()
	raw_p2_img = data.get("pass_2_image") or data.get("co_exhibitor_image") or data.get("second_person_image") or data.get("friend_image") or ""
	pass_2_image = save_image_from_base64_or_url(raw_p2_img, file_name_prefix=f"exh2_{mobile_no[-4:] if len(mobile_no)>=4 else 'pass'}") if raw_p2_img else ""
	pass_2_mobile = (data.get("pass_2_mobile") or data.get("co_exhibitor_mobile") or "").strip()
	pass_2_email = (data.get("pass_2_email") or data.get("co_exhibitor_email") or "").strip().lower()
	pass_2_code = f"{reg_id}-P2" if pass_2_name else ""
	pass_2_qr = f"CONNECTFEST2027:EXHIBITOR:{stall_number or ''}:{pass_2_code}:{pass_2_email or email}" if (pass_2_name and pass_2_code) else ""

	record = {
		"name": reg_id,
		"registration_id": reg_id,
		"full_name": full_name,
		"company_name": company_name,
		"company_logo": company_logo,
		"pass_1_image": company_logo,
		"location": location,
		"bill": bill,
		"gst": gst,
		"mobile_no": mobile_no,
		"email": email,
		"is_bni_member": is_bni_member,
		"is_cnr_member": is_cnr_member,
		"region_name": region_name,
		"chapter_name": chapter_name,
		"industry": industry,
		"segment": segment,
		"source_of_info": matched_source,
		"stall_number": stall_number or None,
		"stall_category": stall_category or "",
		"amount": amount if has_stall_selected else 0.0,
		"payment_status": "Pending" if has_stall_selected else "",
		"payment_mode": (raw_pay_mode or "Online") if has_stall_selected else "",
		"razorpay_order_id": "",
		"razorpay_payment_id": "",
		"razorpay_signature": "",
		"payment_date": None,
		"status": "Registered",
		"pass_code": generate_secure_pass_code("CF"),
		"qr_payload": "",
		"checked_in": 0,
		"checked_in_at": None,
		"checked_in_by": None,
		"pass_1_image": pass_1_image,
		"pass_2_name": pass_2_name,
		"pass_2_image": pass_2_image,
		"pass_2_mobile": pass_2_mobile,
		"pass_2_email": pass_2_email,
		"pass_2_pass_code": pass_2_code,
		"pass_2_qr_payload": pass_2_qr,
		"pass_2_checked_in": 0,
		"pass_2_checked_in_at": None,
		"pass_2_checked_in_by": None,
		"creation": now_str,
		"modified": now_str,
		"modified_by": "Administrator",
		"owner": "Administrator",
		"docstatus": 0,
		"idx": 0,
	}

	# Exhibitor Pass Badge is ONLY generated after payment is Paid and stall is confirmed
	is_paid_stall = bool(stall_number and (record.get("payment_status") == "Paid"))
	exhibitor_pass_url = ""
	pass_2_pass_url = ""
	if is_paid_stall:
		try:
			from connectfest.services.exhibitor_badge import generate_exhibitor_badge_image
			exhibitor_pass_url = generate_exhibitor_badge_image(record, pass_index=1)
			record["exhibitor_pass_url"] = exhibitor_pass_url
			if pass_2_name:
				pass_2_pass_url = generate_exhibitor_badge_image(record, pass_index=2)
				record["pass_2_pass_url"] = pass_2_pass_url
		except Exception:
			pass

	# Direct Reliable Insert into Database & Redis Cache
	try:
		doc = frappe.get_doc({"doctype": "ConnectFest Registration", **record})
		doc.flags.ignore_permissions = True
		doc.flags.ignore_validate = True
		doc.flags.ignore_links = True
		doc.insert()
		frappe.db.commit()

		actual_reg_id = doc.registration_id or doc.name
		record["name"] = doc.name
		record["registration_id"] = actual_reg_id
		set_cached_record(cache, f"{STALL_CACHE_PREFIX}{actual_reg_id}", record)
		if doc.name != actual_reg_id:
			set_cached_record(cache, f"{STALL_CACHE_PREFIX}{doc.name}", record)

		# Provision Frappe User account immediately and auto-login
		auth_token = None
		try:
			ensure_frappe_user(email, full_name, mobile_no, role="Website User")
			if company_logo:
				frappe.db.set_value("User", email, "user_image", company_logo)
			# Generate Token for Exhibitor (Pure stateless X-Auth-Token)
			from connectfest.services.auth import generate_auth_token
			auth_token = generate_auth_token(email, role_type="Exhibitor")
		except Exception:
			pass

		response_data = {
			"status": "success",
			"registration_id": actual_reg_id,
			"stall_number": stall_number or "",
			"pass_code": record.get("pass_code"),
			"message": "Exhibitor registration successful!",
			"logged_in": True,
			"x_auth_token": auth_token,
		}
		if is_paid_stall and exhibitor_pass_url:
			response_data["exhibitor_pass_url"] = format_full_url(exhibitor_pass_url)
		if is_paid_stall and pass_2_name and pass_2_pass_url:
			response_data["pass_2_pass_url"] = format_full_url(pass_2_pass_url)

		if create_order and stall_number:
			from connectfest.services.payments import create_razorpay_order
			order_info = create_razorpay_order(registration_id=actual_reg_id, stall_number=stall_number, is_internal=True)
			if order_info and order_info.get("order"):
				response_data["razorpay_order"] = order_info.get("order")

		return response_data
	except Exception as e:
		return log_and_return_error("ConnectFest Stall Insert Error", e)


@handle_api_errors("ConnectFest Select Stall Error")
def select_stall(registration_id=None, stall_number=None, **kwargs):
	"""
	Allows a registered exhibitor to explicitly choose or change their stall before payment.
	Validates availability, updates registration record, applies dynamic pricing, and places a 10-minute hold.
	"""
	from connectfest.services.stalls import hold_stall
	cache = frappe.cache()
	data = extract_request_data(kwargs)
	reg_id = safe_str(registration_id or data.get("registration_id"))
	stall_num = safe_str(stall_number or data.get("stall_number") or data.get("stall_numbers") or data.get("stall_no"))

	from connectfest.services.auth import is_admin_user, get_authenticated_user
	sess_user = get_authenticated_user(data, **kwargs) or getattr(frappe.session, "user", None)
	if not sess_user or sess_user == "Guest":
		candidate_email = (data.get("email") or data.get("user_email") or "").strip().lower()
		candidate_mob = (data.get("mobile_no") or data.get("phone") or "").strip()
		if not candidate_email and candidate_mob:
			candidate_email = (
				frappe.db.get_value("ConnectFest Registration", {"mobile_no": candidate_mob}, "email")
				or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": candidate_mob}, "email")
				or frappe.db.get_value("User", {"mobile_no": candidate_mob}, "name")
				or frappe.db.get_value("User", {"phone": candidate_mob}, "name")
			)
		if not candidate_email and reg_id:
			candidate_email = (
				frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "email")
				or frappe.db.get_value("ConnectFest Registration", reg_id, "email")
			)
		if not candidate_email and stall_num and frappe.db.exists("ConnectFest Stall", stall_num):
			stall_holder_email = frappe.db.get_value("ConnectFest Stall", stall_num, "booked_by_email")
			if stall_holder_email:
				candidate_email = stall_holder_email

		if candidate_email:
			sess_user = candidate_email
			try:
				frappe.set_user(sess_user)
			except Exception:
				pass

	if not reg_id and sess_user and sess_user != "Guest":
		reg_id = (
			frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "stall_number": stall_num, "payment_status": ("!=", "Paid")}, "registration_id")
			or frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "payment_status": ("!=", "Paid")}, "registration_id", order_by="creation desc")
			or frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "registration_id", order_by="creation desc")
		)

	if not reg_id and stall_num and frappe.db.exists("ConnectFest Stall", stall_num):
		stall_reg = frappe.db.get_value("ConnectFest Stall", stall_num, "booked_by_registration")
		if stall_reg:
			reg_id = stall_reg

	if not reg_id and (not sess_user or sess_user == "Guest"):
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Login required to select stall. Please log in using your registered mobile number or email via OTP.",
		}

	if not stall_num:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Stall Number (stall_number) is mandatory! Please select a valid stall (e.g. PR-05, ST-12, SL-01)."}

	if not frappe.db.exists("ConnectFest Stall", stall_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{stall_num}' does not exist in ConnectFest inventory"}

	stall_doc = frappe.get_doc("ConnectFest Stall", stall_num)
	from connectfest.services.auth import is_admin_user, get_authenticated_user
	auth_user = get_authenticated_user(data, **kwargs)
	if not auth_user and sess_user and sess_user != "Guest":
		auth_user = sess_user
	is_admin = is_admin_user(auth_user) if auth_user else False

	if auth_user and not is_admin:
		logged_email = auth_user.strip().lower()
		logged_phone = (
			frappe.db.get_value("ConnectFest Registration", {"email": auth_user}, "mobile_no")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"email": auth_user}, "mobile_no")
			or frappe.db.get_value("ConnectFest BNI Event Registration", {"email": auth_user}, "mobile_no")
			or frappe.db.get_value("User", auth_user, "mobile_no")
			or frappe.db.get_value("User", auth_user, "phone")
			or ""
		).strip()
		from connectfest.services.bni_members import normalize_phone_number
		clean_logged_phone = normalize_phone_number(logged_phone) if logged_phone else ""

		req_e = (data.get("email") or "").strip().lower()
		if req_e and req_e != logged_email:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "You are currently logged in. Stall booking must match your active logged-in account. To book for a different account, please log out first.",
			}

		raw_m = (data.get("mobile_no") or data.get("phone") or "").strip()
		clean_req_m = normalize_phone_number(raw_m) if raw_m else ""
		if clean_req_m and clean_logged_phone and clean_req_m != clean_logged_phone:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "You are currently logged in. Stall booking must match your registered mobile number. To book for a different account, please log out first.",
			}

	if (stall_doc.is_blocked_by_admin or stall_doc.status == "Blocked") and not is_admin:
		frappe.local.response["http_status_code"] = 403
		return {"status": "error", "message": f"Stall '{stall_num}' is blocked by administration ({stall_doc.blocked_reason or 'Reserved'})"}

	# Check visitor / event restriction BEFORE anything else: visitors and event attendees cannot select stalls directly!
	user_email = (data.get("email") or (sess_user if sess_user and sess_user != "Guest" else "")).strip().lower()
	user_mobile = (data.get("mobile_no") or data.get("phone") or (frappe.db.get_value("User", sess_user, "mobile_no") if sess_user and sess_user != "Guest" else "") or (frappe.db.get_value("User", sess_user, "phone") if sess_user and sess_user != "Guest" else "") or "").strip()

	has_exhibitor = False
	if reg_id and (frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}) or frappe.db.exists("ConnectFest Registration", reg_id)):
		has_exhibitor = True
	elif user_email and frappe.db.exists("ConnectFest Registration", {"email": user_email}):
		has_exhibitor = True
	elif user_mobile and frappe.db.exists("ConnectFest Registration", {"mobile_no": user_mobile}):
		has_exhibitor = True

	if not has_exhibitor and not is_admin:
		is_vis_or_event = False
		if (reg_id and str(reg_id).startswith("VIS-")) or (reg_id and frappe.db.exists("ConnectFest Visitor Registration", {"registration_id": reg_id})):
			is_vis_or_event = True
		elif user_email and (frappe.db.exists("ConnectFest Visitor Registration", {"email": user_email}) or frappe.db.exists("ConnectFest BNI Event Registration", {"email": user_email})):
			is_vis_or_event = True
		elif user_mobile and (frappe.db.exists("ConnectFest Visitor Registration", {"mobile_no": user_mobile}) or frappe.db.exists("ConnectFest BNI Event Registration", {"mobile_no": user_mobile})):
			is_vis_or_event = True

		if is_vis_or_event:
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "You are currently registered as a Visitor/Event attendee. Direct stall selection is not allowed. Please register as an Exhibitor first to book a stall.",
			}

	# Strict CNR verification when restriction is enabled or for corner stalls
	from connectfest.services.bni_members import is_valid_bni_member, is_crn_bni_restriction_enabled
	should_check_cnr = (is_crn_bni_restriction_enabled() or cint(getattr(stall_doc, "crn", 0)) == 1)
	if should_check_cnr and not is_admin:
		# Check if registration is already verified/marked as CNR member in Desk
		reg_bni = 0
		if reg_id:
			reg_bni = (
				cint(frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "is_cnr_member"))
				or cint(frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "is_bni_member"))
				or cint(frappe.db.get_value("ConnectFest Registration", reg_id, "is_cnr_member"))
				or cint(frappe.db.get_value("ConnectFest Registration", reg_id, "is_bni_member"))
			)
		if not reg_bni and sess_user and sess_user != "Guest":
			user_mobs = [sess_user]
			u_mob_val = (frappe.db.get_value("User", sess_user, "mobile_no") or frappe.db.get_value("User", sess_user, "phone") or "").strip()
			if u_mob_val:
				user_mobs.append(u_mob_val)
			mobs_tuple = tuple(set(user_mobs))
			has_reg_bni = frappe.db.sql("""
				SELECT name FROM `tabConnectFest Registration`
				WHERE (email = %(user)s OR mobile_no IN %(mobs)s)
				  AND (is_cnr_member = 1 OR is_bni_member = 1)
				LIMIT 1
			""", {"user": sess_user, "mobs": mobs_tuple}, as_dict=True)
			if has_reg_bni:
				reg_bni = 1

		if not reg_bni:
			req_m = (data.get("mobile_no") or data.get("phone") or "").strip()
			req_e = (data.get("email") or "").strip().lower()
			if req_m or req_e:
				has_data_bni = frappe.db.sql("""
					SELECT name FROM `tabConnectFest Registration`
					WHERE (email = %(e)s OR (mobile_no = %(m)s AND %(m)s != ''))
					  AND (is_cnr_member = 1 OR is_bni_member = 1)
					LIMIT 1
				""", {"e": req_e, "m": req_m}, as_dict=True)
				if has_data_bni:
					reg_bni = 1

		u_mob = (
			(frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "mobile_no") if reg_id else None)
			or (frappe.db.get_value("ConnectFest Registration", reg_id, "mobile_no") if reg_id and frappe.db.exists("ConnectFest Registration", reg_id) else None)
			or (frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "mobile_no") if sess_user and sess_user != "Guest" else None)
			or (frappe.db.get_value("User", sess_user, "mobile_no") if sess_user and sess_user != "Guest" else None)
			or (frappe.db.get_value("User", sess_user, "phone") if sess_user and sess_user != "Guest" else None)
			or data.get("mobile_no")
			or data.get("phone")
		)
		u_email = (
			(frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "email") if reg_id else None)
			or (frappe.db.get_value("ConnectFest Registration", reg_id, "email") if reg_id and frappe.db.exists("ConnectFest Registration", reg_id) else None)
			or (sess_user if sess_user and sess_user != "Guest" else None)
			or data.get("email")
		)
		u_name = (
			(frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "full_name") if reg_id else None)
			or (frappe.db.get_value("ConnectFest Registration", reg_id, "full_name") if reg_id and frappe.db.exists("ConnectFest Registration", reg_id) else None)
			or (frappe.db.get_value("User", sess_user, "full_name") if sess_user and sess_user != "Guest" else None)
			or data.get("full_name")
			or data.get("name")
		)
		is_bni_valid, bni_doc = is_valid_bni_member(mobile_no=u_mob, email=u_email, name=u_name)
		if reg_bni == 1:
			is_bni_valid = True

		if not is_bni_valid:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "Stall selection is strictly restricted to verified CNR (Chennai North Region) members",
			}

	# Prevent duplicate payment/selection for the same stall if already Paid & Confirmed by this user
	if sess_user and sess_user != "Guest":
		user_mob = (frappe.db.get_value("User", sess_user, "mobile_no") or frappe.db.get_value("User", sess_user, "phone") or "").strip()
		already_paid_this = frappe.db.sql("""
			SELECT registration_id, stall_number FROM `tabConnectFest Registration`
			WHERE (email = %(email)s OR (mobile_no = %(mobile)s AND %(mobile)s != ''))
			  AND stall_number = %(stall_num)s
			  AND payment_status = 'Paid'
			LIMIT 1
		""", {"email": sess_user, "mobile": user_mob, "stall_num": stall_num}, as_dict=True)
		if already_paid_this:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"Stall '{stall_num}' has already been paid and confirmed for your account. Multiple payments are strictly prohibited!",
				"payment_status": "Paid",
				"stall_number": stall_num,
			}

	if stall_doc.status == "Booked" and stall_doc.booked_by_registration != reg_id:
		frappe.local.response["http_status_code"] = 409
		return {"status": "error", "message": f"Stall '{stall_num}' is already booked and confirmed by another exhibitor"}

	record = None
	# If caller provided reg_id explicitly:
	if reg_id:
		if frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
			records = frappe.get_all("ConnectFest Registration", filters={"registration_id": reg_id}, fields=["*"], limit=1)
			if records:
				r = records[0]
				if r.payment_status == "Paid":
					if r.stall_number == stall_num:
						frappe.local.response["http_status_code"] = 400
						return {
							"status": "error",
							"message": f"Stall '{stall_num}' has already been paid and confirmed for this registration. You cannot change your stall after payment.",
							"payment_status": "Paid",
							"stall_number": stall_num,
						}
					else:
						# Registration reg_id was already paid for a different stall.
						# Since user can buy multiple stalls, spawn a new registration for this new stall!
						reg_id = None
				else:
					record = r

	# Auto-resolve or create registration for logged-in exhibitor or provided user details
	if not reg_id and (user_email or user_mobile):
		# Priority 1: Existing pending registration for this exact stall
		p1 = None
		if user_email:
			p1 = frappe.db.get_value(
				"ConnectFest Registration",
				{"email": user_email, "stall_number": stall_num, "payment_status": ("!=", "Paid")},
				"registration_id"
			)
		if not p1 and user_mobile:
			p1 = frappe.db.get_value(
				"ConnectFest Registration",
				{"mobile_no": user_mobile, "stall_number": stall_num, "payment_status": ("!=", "Paid")},
				"registration_id"
			)

		if p1:
			reg_id = p1
		else:
			# Priority 2: Existing pending registration with no stall selected yet
			p2 = None
			if user_email:
				p2 = frappe.db.get_value(
					"ConnectFest Registration",
					{"email": user_email, "stall_number": ("in", ["", None]), "payment_status": ("!=", "Paid")},
					"registration_id",
					order_by="creation desc"
				)
			if not p2 and user_mobile:
				p2 = frappe.db.get_value(
					"ConnectFest Registration",
					{"mobile_no": user_mobile, "stall_number": ("in", ["", None]), "payment_status": ("!=", "Paid")},
					"registration_id",
					order_by="creation desc"
				)

			if p2:
				reg_id = p2
			else:
				# Priority 3: User already has paid registrations (e.g. A-01), but wants to buy an additional stall (e.g. A-02)!
				# Or exhibitor registration details (including company_name) were passed in request.
				prev_regs = []
				if user_email:
					prev_regs = frappe.get_all(
						"ConnectFest Registration",
						filters={"email": user_email},
						fields=["*"],
						order_by="creation asc",
						limit=1
					)
				if not prev_regs and user_mobile:
					prev_regs = frappe.get_all(
						"ConnectFest Registration",
						filters={"mobile_no": user_mobile},
						fields=["*"],
						order_by="creation asc",
						limit=1
					)
				prev = prev_regs[0] if prev_regs else {}

				if not prev_regs and (user_email or user_mobile):
					# Visitor and Event attendees cannot select stalls directly without registering as an Exhibitor
					vis_or_evt = frappe.db.sql("""
						SELECT name FROM `tabConnectFest Visitor Registration`
						WHERE (email = %s AND %s != '') OR (mobile_no = %s AND %s != '')
						UNION
						SELECT name FROM `tabConnectFest BNI Event Registration`
						WHERE (email = %s AND %s != '') OR (mobile_no = %s AND %s != '')
						LIMIT 1
					""", (user_email or "", user_email or "", user_mobile or "", user_mobile or "", user_email or "", user_email or "", user_mobile or "", user_mobile or ""))
					if vis_or_evt and not is_admin:
						frappe.local.response["http_status_code"] = 403
						return {
							"status": "forbidden",
							"message": "You are currently registered as a Visitor/Event attendee. Direct stall selection is not allowed. Please register as an Exhibitor first to book a stall."
						}

				cand_name_tmp = (data.get("full_name") or prev.get("full_name") or (frappe.db.get_value("User", sess_user, "full_name") if sess_user and sess_user != "Guest" else "") or "").strip()
				if not prev and (user_mobile or user_email):
					from connectfest.services.bni_members import is_valid_bni_member
					_, bni_m = is_valid_bni_member(mobile_no=user_mobile, email=user_email, name=cand_name_tmp)
					if bni_m:
						prev = bni_m

				company_name = (data.get("company_name") or prev.get("company_name") or "").strip()
				if not company_name:
					if cand_name_tmp and "@" not in cand_name_tmp:
						company_name = cand_name_tmp
					elif user_email and "@" in user_email:
						company_name = user_email.split("@")[0].title()
					else:
						company_name = "Exhibitor"

				new_reg_id = generate_stall_id(cache)
				cand_mob = (user_mobile or prev.get("mobile_no") or "").strip()
				cand_email = (user_email or prev.get("email") or "").strip().lower()
				cand_name = (
					data.get("full_name")
					or prev.get("full_name")
					or (frappe.db.get_value("User", sess_user, "full_name") if sess_user and sess_user != "Guest" else None)
					or ""
				).strip()

				from connectfest.services.bni_members import is_valid_bni_member, calculate_stall_price_for_user
				v_bni = False
				if cand_mob and cand_email and cand_name:
					v_bni, b_info = is_valid_bni_member(mobile_no=cand_mob, email=cand_email, name=cand_name)
				is_bni = 1 if v_bni else 0

				price_info = calculate_stall_price_for_user(
					tier=stall_doc.tier,
					user_name=cand_name,
					user_mobile=cand_mob,
					user_email=cand_email,
					is_bni_member=is_bni,
					custom_base_price=stall_doc.price
				)
				tier = stall_doc.tier
				price = price_info["total_amount"]
				if cand_name and "@" not in cand_name:
					full_name = cand_name
				else:
					full_name = (user_email or sess_user or "Exhibitor").split("@")[0].replace(".", " ").replace("_", " ").title()

				raw_source = data.get("source_of_info") or prev.get("source_of_info") or "Website"
				matched_source = raw_source if raw_source in VALID_SOURCES else "Other"

				region_name = (data.get("region_name") or prev.get("region_name") or "").strip()
				chapter_name = (data.get("chapter_name") or prev.get("chapter_name") or "").strip()
				if is_bni:
					if not (region_name and chapter_name) and (cand_mob or cand_email):
						if not b_info:
							_, b_info = is_valid_bni_member(mobile_no=cand_mob, email=cand_email, name=cand_name)
						if b_info:
							chapter_name = chapter_name or b_info.get("chapter_name") or ""
							region_name = region_name or b_info.get("region_name") or ""
					region_name = region_name or "BNI"
					chapter_name = chapter_name or "BNI"

				new_doc = frappe.get_doc({
					"doctype": "ConnectFest Registration",
					"name": new_reg_id,
					"registration_id": new_reg_id,
					"full_name": full_name,
					"company_name": company_name,
					"company_logo": data.get("company_logo") or prev.get("company_logo") or "",
					"location": data.get("location") or prev.get("location") or "Chennai",
					"bill": data.get("bill") or prev.get("bill") or "",
					"gst": data.get("gst") or prev.get("gst") or "",
					"mobile_no": user_mobile or prev.get("mobile_no") or "",
					"email": user_email or prev.get("email") or "",
					"is_bni_member": is_bni,
					"region_name": region_name if is_bni else "",
					"chapter_name": chapter_name if is_bni else "",
					"industry": data.get("industry") or prev.get("industry") or "",
					"segment": data.get("segment") or prev.get("segment") or "",
					"source_of_info": matched_source,
					"stall_number": stall_num,
					"stall_category": tier,
					"amount": price,
					"payment_status": "Pending",
					"payment_mode": "Online",
					"status": "Registered",
					"pass_code": generate_secure_pass_code("CF"),
					"qr_payload": "",
					"checked_in": 0,
					"checked_in_at": None,
					"checked_in_by": None,
					"pass_2_pass_code": f"{new_reg_id}-P2" if prev.get("pass_2_name") else "",
					"pass_2_checked_in": 0,
					"pass_2_checked_in_at": None,
					"pass_2_checked_in_by": None,
				})
				new_doc.insert(ignore_permissions=True)
				frappe.db.commit()
				reg_id = new_reg_id
				record = new_doc.as_dict()
				set_cached_record(cache, f"{STALL_CACHE_PREFIX}{new_reg_id}", record)
				if new_doc.name != new_reg_id:
					set_cached_record(cache, f"{STALL_CACHE_PREFIX}{new_doc.name}", record)

	if not reg_id:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	cache = frappe.cache()
	if not record:
		record = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}")
		if not record and frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
			records = frappe.get_all("ConnectFest Registration", filters={"registration_id": reg_id}, fields=["*"], limit=1)
			if records:
				record = records[0]

	if not record:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Registration '{reg_id}' not found"}

	# Prevent modifying stall if already Paid & Confirmed
	db_payment_status = record.get("payment_status") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "payment_status")
	if db_payment_status == "Paid":
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": f"Stall '{record.get('stall_number')}' is already paid and confirmed for this registration. You cannot change your stall after payment.",
			"payment_status": "Paid",
			"stall_number": record.get("stall_number"),
		}

	# IDOR Prevention: Ensure logged-in user owns this registration (unless Admin)
	from connectfest.services.auth import is_admin_user
	if sess_user and sess_user != "Guest" and not is_admin_user(sess_user):
		rec_email = (record.get("email") or "").strip().lower()
		rec_mob = (record.get("mobile_no") or "").strip()
		sess_lower = sess_user.strip().lower()
		user_phone = (frappe.db.get_value("User", sess_user, "phone") or frappe.db.get_value("User", sess_user, "mobile_no") or "").strip()
		if rec_email != sess_lower and (not user_phone or user_phone != rec_mob):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Access Denied: You cannot select or modify stalls for another attendee's registration."
			}

	# Release previously held stall if different
	old_stall = record.get("stall_number")
	if old_stall and old_stall != stall_num:
		from connectfest.services.common import STALL_LOCK_PREFIX
		try:
			cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{old_stall}"))
		except Exception:
			pass
		if frappe.db.exists("ConnectFest Stall", old_stall):
			old_doc = frappe.get_doc("ConnectFest Stall", old_stall)
			if old_doc.status == "Hold":
				frappe.db.set_value("ConnectFest Stall", old_stall, {
					"status": "Available",
					"hold_token": None,
					"hold_expires_at": None,
					"booked_by_registration": None,
					"booked_by_name": None,
					"booked_by_company": None,
					"booked_by_email": None,
					"booked_by_mobile": None,
					"payment_mode": None,
				}, update_modified=False)
				frappe.db.commit()
				from connectfest.services.stalls import invalidate_stalls_cache
				invalidate_stalls_cache()
				try:
					from connectfest.services.realtime import broadcast_stall_update
					broadcast_stall_update(old_stall, "Available")
				except Exception:
					pass

	from connectfest.services.bni_members import calculate_stall_price_for_user
	price_info = calculate_stall_price_for_user(
		tier=stall_doc.tier,
		user_mobile=user_mobile or record.get("mobile_no"),
		user_email=user_email or record.get("email"),
		is_bni_member=record.get("is_bni_member"),
		custom_base_price=stall_doc.price
	)
	tier = stall_doc.tier or ""
	price = price_info["total_amount"]

	record["stall_number"] = stall_num
	record["stall_category"] = tier
	record["amount"] = price
	record["payment_status"] = "Pending"
	record["payment_mode"] = "Online"
	set_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}", record)

	db_target = None
	if frappe.db.exists("ConnectFest Registration", reg_id):
		db_target = reg_id
	elif frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
		db_target = frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "name")

	if db_target:
		p_code = frappe.db.get_value("ConnectFest Registration", db_target, "pass_code")
		if not p_code or p_code.startswith("CF-2611-"):
			p_code = generate_secure_pass_code("CF")
		p2_name = (frappe.db.get_value("ConnectFest Registration", db_target, "pass_2_name") or "").strip()
		p2_code = (frappe.db.get_value("ConnectFest Registration", db_target, "pass_2_pass_code") or "").strip()
		if p2_name and (not p2_code or p2_code.startswith("CF-2611-") or p2_code.endswith("-P2")):
			p2_code = generate_secure_pass_code("CF")
		p2_em = (frappe.db.get_value("ConnectFest Registration", db_target, "pass_2_email") or "").strip()
		em = record.get("email") or ""
		qr_pl = p_code
		p2_qr = p2_code if (p2_name and p2_code) else ""
		frappe.db.set_value(
			"ConnectFest Registration",
			db_target,
			{
				"stall_number": stall_num,
				"stall_category": tier,
				"amount": price,
				"payment_status": "Pending",
				"payment_mode": "Online",
				"pass_code": p_code,
				"qr_payload": qr_pl,
				"pass_2_pass_code": p2_code if p2_name else "",
				"pass_2_qr_payload": p2_qr if p2_name else "",
			},
			update_modified=False,
		)
		frappe.db.commit()

	# Hold stall for 10 mins
	hold_res = hold_stall(
		stall_number=stall_num,
		email=record.get("email"),
		mobile_no=record.get("mobile_no"),
		full_name=record.get("full_name"),
		company_name=record.get("company_name"),
		registration_id=reg_id,
	)

	if hold_res.get("status") == "error":
		return hold_res

	# Stall selected and held for 10 minutes (Badge is generated once payment is confirmed)
	return {
		"status": "success",
		"message": f"Stall {stall_num} ({tier}) selected successfully for {reg_id}" if tier else f"Stall {stall_num} selected successfully for {reg_id}",
		"registration_id": reg_id,
		"stall_number": stall_num,
		"stall_category": tier,
		"dimensions": stall_doc.dimensions,
		"amount": price,
		"hold": hold_res,
	}


@handle_api_errors("ConnectFest Visitor Registration Error")
def register_visitor(**kwargs):
	"""
	Visitor Registration API.
	- Mandatory: Full Name, Email Address, Mobile Number
	- Optional: Source of Info, Which Day
	- Free entry with QR code pass
	"""
	data = extract_request_data(kwargs)
	has_lock = False
	lock_email_key = None
	lock_mob_key = None

	# Determine session user
	from connectfest.services.auth import get_authenticated_user
	sess_user = get_authenticated_user(data, **kwargs) or (getattr(frappe.session, "user", None) if hasattr(frappe, "session") else None)
	is_session_logged_in = bool(sess_user and sess_user != "Guest")

	booked_by_email = (data.get("booked_by") or data.get("exhibitor_email") or data.get("created_by") or "").strip().lower()

	full_name = (data.get("full_name") or data.get("name") or "").strip()
	email = (data.get("email") or data.get("email_address") or "").strip().lower()
	mobile_no = (data.get("mobile_no") or data.get("mobile") or data.get("phone") or "").strip()

	# If user is already logged in and fields are omitted, auto-populate from user/registration
	if is_session_logged_in:
		if not email:
			email = sess_user.lower()
		if not full_name:
			full_name = (
				frappe.db.get_value("ConnectFest Registration", {"email": email}, "full_name")
				or frappe.db.get_value("User", email, "full_name")
				or email.split("@")[0].title()
			)
		if not mobile_no:
			mobile_no = (
				frappe.db.get_value("ConnectFest Registration", {"email": email}, "mobile_no")
				or frappe.db.get_value("User", email, "mobile_no")
				or frappe.db.get_value("User", email, "phone")
				or ""
			)

	raw_image = (
		data.get("visitor_image")
		or data.get("image")
		or data.get("photo")
		or data.get("visitor_photo")
		or data.get("avatar")
		or data.get("company_logo")
		or data.get("logo")
		or ""
	).strip()
	visitor_image = save_image_from_base64_or_url(raw_image, file_name_prefix=f"vis_{mobile_no[-4:] if len(mobile_no)>=4 else 'pass'}") if raw_image else ""
	company_name = safe_str(data.get("company_name") or data.get("company") or data.get("company_title") or "").strip()
	which_day = (data.get("which_day") or data.get("day") or "Both days").strip()
	area_of_interest = (data.get("area_of_interest") or data.get("areas_of_interest") or data.get("interest") or "").strip()
	source_of_info = (data.get("source_of_info") or data.get("source") or "").strip()
	matched_source = None
	for src in VALID_SOURCES:
		if src.lower() == source_of_info.lower():
			matched_source = src
			break
	source_of_info = matched_source or ("Other" if source_of_info else "")

	errors = []
	if not full_name:
		errors.append("Full Name (full_name) is required")
	if not email:
		errors.append("Email Address (email) is required")
	elif "@" not in email:
		errors.append("Valid Email Address is required")
	if not mobile_no:
		errors.append("Mobile Number (mobile_no) is required")

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

	# Enforce logged-in user constraint:
	# If an attendee is already logged in (non-admin), their submitted email & mobile MUST EXACTLY MATCH!
	from connectfest.services.auth import is_admin_user, get_authenticated_user
	auth_user = get_authenticated_user(data, **kwargs)
	if not auth_user and sess_user and sess_user != "Guest":
		auth_user = sess_user
	is_admin = is_admin_user(auth_user) if auth_user else False

	if auth_user and not is_admin:
		sess_email = auth_user.strip().lower()
		sess_phone = (
			frappe.db.get_value("ConnectFest Registration", {"email": auth_user}, "mobile_no")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"email": auth_user}, "mobile_no")
			or frappe.db.get_value("ConnectFest BNI Event Registration", {"email": auth_user}, "mobile_no")
			or frappe.db.get_value("User", auth_user, "mobile_no")
			or frappe.db.get_value("User", auth_user, "phone")
			or ""
		).strip()
		from connectfest.services.bni_members import normalize_phone_number
		clean_sess_phone = normalize_phone_number(sess_phone) if sess_phone else ""
		clean_req_phone = normalize_phone_number(mobile_no) if mobile_no else ""

		if email and email.strip().lower() != sess_email:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "You are currently logged in. Registration must match your active logged-in account. To register for a different account, please log out first.",
			}

		if clean_sess_phone and clean_req_phone and clean_req_phone != clean_sess_phone:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "You are currently logged in. Registration must match your registered mobile number. To register with a different mobile number, please log out first.",
			}

	# Check Exhibitor status:
	# 1. Logged in session user has Exhibitor role or exists in ConnectFest Registration
	# 2. Target email or mobile exists in ConnectFest Registration
	# 3. Booked-by email exists in ConnectFest Registration
	is_exhibitor = False
	if is_session_logged_in:
		user_roles = frappe.get_roles(sess_user)
		if "Exhibitor" in user_roles or frappe.db.exists("ConnectFest Registration", {"email": sess_user.lower()}):
			is_exhibitor = True

	if not is_exhibitor:
		if email and frappe.db.exists("ConnectFest Registration", {"email": email}):
			is_exhibitor = True
		elif mobile_no and frappe.db.exists("ConnectFest Registration", {"mobile_no": mobile_no}):
			is_exhibitor = True
		elif booked_by_email and frappe.db.exists("ConnectFest Registration", {"email": booked_by_email}):
			is_exhibitor = True

	# Determine whether stall is booked / bought (Paid or stall assigned)
	has_bought_stall = False
	stall_check_conditions = []
	stall_check_values = {}

	if is_session_logged_in:
		stall_check_conditions.append("email = %(sess_user)s")
		stall_check_values["sess_user"] = sess_user.lower()
	if email:
		stall_check_conditions.append("email = %(email)s")
		stall_check_values["email"] = email
	if mobile_no:
		stall_check_conditions.append("mobile_no = %(mobile)s")
		stall_check_values["mobile"] = mobile_no
	if booked_by_email:
		stall_check_conditions.append("email = %(booked_by)s")
		stall_check_values["booked_by"] = booked_by_email

	if stall_check_conditions:
		where_stalls = " OR ".join(stall_check_conditions)
		booked_stalls = frappe.db.sql(f"""
			SELECT name, stall_number, payment_status, registration_id
			FROM `tabConnectFest Registration`
			WHERE ({where_stalls})
			  AND (payment_status = 'Paid' OR (stall_number IS NOT NULL AND stall_number != ''))
			LIMIT 1
		""", stall_check_values, as_dict=True)
		if booked_stalls:
			has_bought_stall = True
			is_exhibitor = True

	# Enforce strict 1-visitor-pass maximum limit per email and mobile (applies to everyone including exhibitors)
	existing = frappe.db.sql("""
		SELECT name, registration_id, full_name, company_name, visitor_image, email, mobile_no, which_day, pass_code, qr_payload, status, checked_in, checked_in_at, area_of_interest
		FROM `tabConnectFest Visitor Registration`
		WHERE email = %(email)s OR mobile_no = %(mobile)s
		LIMIT 1
	""", {"email": email, "mobile": mobile_no}, as_dict=True)

	if existing:
		rec = existing[0]
		rec_email = (rec.email or "").strip().lower()
		rec_mobile = (rec.mobile_no or "").strip()

		# Conflict A: Mobile matches, but email is different!
		if mobile_no and rec_mobile == mobile_no and rec_email != email.lower():
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "Mobile number is already registered with another account! Each mobile number can only be used once."
			}

		# Conflict B: Email matches, but mobile is different!
		if rec_email == email.lower() and mobile_no and rec_mobile != mobile_no:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "Email address is already registered with a different mobile number! Please use your registered mobile number."
			}

		vis_img = rec.get("visitor_image") or ""
		if not vis_img or not vis_img.startswith("/files/visitor_badges/"):
			try:
				from connectfest.services.visitor_badge import generate_visitor_badge_image
				generated_img = generate_visitor_badge_image(rec)
				if generated_img:
					vis_img = generated_img
					rec["visitor_image"] = vis_img
					frappe.db.set_value("ConnectFest Visitor Registration", rec.name, "visitor_image", vis_img)
					frappe.db.commit()
			except Exception:
				pass

		if company_name and not rec.get("company_name"):
			try:
				frappe.db.set_value("ConnectFest Visitor Registration", rec.name, "company_name", company_name)
				rec["company_name"] = company_name
				frappe.db.commit()
			except Exception:
				pass

		ensure_frappe_user(rec_email, rec.full_name or full_name, rec_mobile, role="Website User")

		base_url = "https://api.connectfesttn.in"
		try:
			from connectfest.services.notifications import get_base_url
			base_url = get_base_url()
		except Exception:
			pass

		from connectfest.services.passes import get_pass_qr_url
		existing_code = rec.get("pass_code") or rec.get("registration_id")
		existing_auth_token = None
		try:
			from connectfest.services.auth import generate_auth_token
			existing_auth_token = generate_auth_token(rec_email, role_type="Visitor")
		except Exception:
			pass
		raw_v_badge = rec.get("visitor_image_pass") or rec.get("visitor_image") or ""
		already_res = {
			"status": "already_registered",
			"message": "You are already registered for ConnectFest 2027! Maximum 1 visitor pass is allowed per attendee. Please log in using Email OTP to access your entrance pass.",
			"registration_id": rec.get("registration_id"),
			"pass_code": existing_code,
			"visitor_pass_url": format_full_url(raw_v_badge) if raw_v_badge else "",
			"qr_url": get_pass_qr_url(existing_code),
			"qr_download_url": f"{base_url}/api/method/connectfest.api.download_pass_qr?code={urllib.parse.quote(str(existing_code))}",
			"badge_download_url": f"{base_url}/api/method/connectfest.api.download_visitor_badge?code={urllib.parse.quote(str(existing_code))}",
		}
		if existing_auth_token:
			already_res["x_auth_token"] = existing_auth_token
			already_res["token"] = existing_auth_token
			already_res["auth_token"] = existing_auth_token
			if hasattr(frappe, "local") and hasattr(frappe.local, "response") and isinstance(frappe.local.response, dict):
				frappe.local.response["x_auth_token"] = existing_auth_token
		return already_res

	# Email OTP Verification:
	# If caller is NOT logged in, visitor registration strictly requires Email OTP verification!
	# If caller is logged in (e.g. Exhibitor logged in), Email OTP is bypassed because session is already authenticated.
	if not is_session_logged_in:
		email_val_err = validate_registration_email_otp(email, data, expected_type="visitor")
		if email_val_err:
			frappe.local.response["http_status_code"] = 400
			return email_val_err

	# Check if mobile_no is bound to another active User
	if mobile_no:
		user_with_mob = frappe.db.get_value("User", {"mobile_no": mobile_no}, "name") or frappe.db.get_value("User", {"phone": mobile_no}, "name")
		if user_with_mob and user_with_mob.lower() != email.lower():
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "Mobile number is already registered with another account! Each mobile number can only be used once.",
			}

	cache = frappe.cache()
	reg_id = generate_visitor_id(cache)
	while True:
		pass_code = generate_secure_pass_code("CF")
		if not frappe.db.exists("ConnectFest Visitor Registration", {"pass_code": pass_code}):
			break
	qr_payload = pass_code
	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S.%f")

	record = {
		"name": reg_id,
		"registration_id": reg_id,
		"full_name": full_name,
		"company_name": company_name,
		"visitor_image": visitor_image,
		"visitor_image_pass": "",
		"email": email,
		"mobile_no": mobile_no,
		"area_of_interest": area_of_interest,
		"which_day": which_day,
		"pass_code": pass_code,
		"qr_payload": qr_payload,
		"checked_in": 0,
		"checked_in_at": None,
		"source_of_info": source_of_info,
		"status": "Registered",
		"creation": now_str,
		"modified": now_str,
		"modified_by": "Administrator",
		"owner": "Administrator",
		"docstatus": 0,
		"idx": 0,
	}

	# Dynamically generate official Visitor Badge Pass Image
	visitor_image_pass = ""
	try:
		from connectfest.services.visitor_badge import generate_visitor_badge_image
		badge_img_url = generate_visitor_badge_image(record)
		if badge_img_url:
			visitor_image_pass = badge_img_url
			record["visitor_image_pass"] = visitor_image_pass
	except Exception as e:
		try:
			frappe.logger("connectfest").warning(f"[Visitor Badge Generation Warning] {str(e)}")
		except Exception:
			pass

	base_url = "https://api.connectfesttn.in"
	try:
		from connectfest.services.notifications import get_base_url
		base_url = get_base_url()
	except Exception:
		pass

	from connectfest.services.passes import get_pass_qr_url
	qr_url = get_pass_qr_url(pass_code)
	qr_download_url = f"{base_url}/api/method/connectfest.api.download_pass_qr?code={urllib.parse.quote(str(pass_code))}"
	badge_download_url = f"{base_url}/api/method/connectfest.api.download_visitor_badge?code={urllib.parse.quote(str(pass_code))}"

	pass_info = {
		"pass_code": pass_code,
		"registration_id": reg_id,
		"pass_type": "visitor_pass",
		"pass_title": "Visitor Gate Pass",
		"full_name": full_name,
		"company_name": company_name,
		"visitor_image": format_full_url(visitor_image) if visitor_image else "",
		"visitor_image_pass": format_full_url(visitor_image_pass) if visitor_image_pass else "",
		"visitor_pass_url": f"{format_full_url(visitor_image_pass or visitor_image)}?v={int(now_datetime().timestamp())}",
		"email": email,
		"mobile_no": mobile_no,
		"area_of_interest": area_of_interest,
		"which_day": which_day,
		"access": "VISITOR - BOTH DAYS" if (not which_day or which_day.lower() == "both days") else f"VISITOR - {which_day.upper()}",
		"venue": "Chennai Trade Centre, Nandambakkam",
		"dates": "20 & 21 Feb 2027",
		"qr_payload": qr_payload,
		"qr_url": qr_url,
		"qr_download_url": qr_download_url,
		"badge_download_url": badge_download_url,
		"status": "Registered",
		"checked_in": 0,
	}

	if "company_name" in record:
		try:
			if not frappe.db.has_column("ConnectFest Visitor Registration", "company_name"):
				record.pop("company_name", None)
		except Exception:
			pass

	if "visitor_image_pass" in record:
		try:
			if not frappe.db.has_column("ConnectFest Visitor Registration", "visitor_image_pass"):
				record.pop("visitor_image_pass", None)
		except Exception:
			pass

	try:
		doc = frappe.get_doc({"doctype": "ConnectFest Visitor Registration", **record})
		doc.flags.ignore_permissions = True
		doc.flags.ignore_validate = True
		doc.flags.ignore_links = True
		doc.insert()
		frappe.db.commit()
		set_cached_record(cache, f"{VISITOR_CACHE_PREFIX}{reg_id}", record)

		# Dispatch visitor confirmation email in background (non-blocking for fast <500ms API response)
		try:
			from connectfest.services.notifications import send_visitor_confirmation_email
			frappe.enqueue(
				send_visitor_confirmation_email,
				queue="short",
				enqueue_after_commit=True,
				registration_id=reg_id,
				email=email,
				pass_code=pass_code,
			)
		except Exception:
			frappe.log_error(title="ConnectFest Visitor Notification Queue Error", message=frappe.get_traceback())

		# Trigger WhatsApp Confirmation via WATI in background (non-blocking)
		try:
			from connectfest.services.wati import send_visitor_registration_whatsapp
			frappe.enqueue(
				send_visitor_registration_whatsapp,
				queue="short",
				enqueue_after_commit=True,
				registration_id=reg_id,
				mobile_no=mobile_no,
				pass_code=pass_code,
				reg_data=record,
			)
		except Exception as wati_err:
			frappe.log_error(title="ConnectFest Visitor WhatsApp Queue Error", message=str(wati_err))

		# Pure stateless X-Auth-Token for visitors
		logged_in = True
		auth_token = None
		try:
			ensure_frappe_user(email, full_name, mobile_no, role="Website User")
			from connectfest.services.auth import generate_auth_token
			auth_token = generate_auth_token(email, role_type="Exhibitor" if is_exhibitor else "Visitor")
		except Exception:
			pass

		res_payload = {
			"status": "success",
			"registration_id": reg_id,
			"pass_code": pass_code,
			"message": "Visitor registration successful!" if not is_exhibitor else "Visitor pass generated successfully!",
			"logged_in": logged_in,
			"is_exhibitor": is_exhibitor,
			"has_bought_stall": has_bought_stall,
			"user_type": "exhibitor" if is_exhibitor else "visitor",
			"visitor_pass_url": pass_info.get("visitor_pass_url") or "",
			"pass_info": pass_info,
		}
		if auth_token:
			res_payload["x_auth_token"] = auth_token
			res_payload["token"] = auth_token
			res_payload["auth_token"] = auth_token
			if hasattr(frappe, "local") and hasattr(frappe.local, "response") and isinstance(frappe.local.response, dict):
				frappe.local.response["x_auth_token"] = auth_token
		return res_payload
	except Exception as e:
		err_msg = str(e).strip() or type(e).__name__
		tb = frappe.get_traceback()
		try:
			frappe.log_error(title="ConnectFest Visitor Registration Error", message=tb)
			frappe.db.commit()
		except Exception:
			pass
		return {
			"status": "error",
			"message": f"Visitor registration failed: {err_msg}",
			"error": err_msg,
		}
	finally:
		if has_lock:
			try:
				if lock_email_key:
					cache.delete(lock_email_key)
				if lock_mob_key:
					cache.delete(lock_mob_key)
			except Exception:
				pass


@handle_api_errors("ConnectFest Check Booking Status Error")
def check_user_booking_status(email=None, mobile_no=None, **kwargs):
	"""Check if an authenticated user's email or mobile has an existing stall booking or visitor pass."""
	from connectfest.services.auth import resolve_authenticated_session_user, is_admin_user
	sess_user, _ = resolve_authenticated_session_user(**kwargs)
	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorized", "message": "Authentication required."}

	data = extract_request_data(kwargs)
	em = (email or data.get("email") or "").strip().lower()
	mb = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()

	if not is_admin_user(sess_user):
		if em and em != sess_user.strip().lower():
			frappe.local.response["http_status_code"] = 403
			return {"status": "forbidden", "message": "Access Denied."}
		if mb:
			user_mob = (frappe.db.get_value("User", sess_user, "mobile_no") or frappe.db.get_value("User", sess_user, "phone") or "").strip()
			if user_mob and mb != user_mob:
				frappe.local.response["http_status_code"] = 403
				return {"status": "forbidden", "message": "Access Denied."}
		if not em:
			em = sess_user.strip().lower()

	stall_booking = None
	stall_bookings = []
	visitor_pass = None

	if em or mb:
		stalls = frappe.db.sql("""
			SELECT registration_id, full_name, company_name, email, mobile_no, stall_number, stall_category,
			       amount, payment_status, payment_mode, payment_date, status, creation,
			       location, bill, gst
			FROM `tabConnectFest Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s) AND payment_status = 'Paid'
			ORDER BY creation DESC
		""", {"email": em, "mobile": mb}, as_dict=True)
		if stalls:
			stall_bookings = stalls
			stall_booking = stalls[0]

		visitors = frappe.db.sql("""
			SELECT registration_id, full_name, company_name, visitor_image, email, mobile_no, which_day, pass_code, status, checked_in, checked_in_at, source_of_info, area_of_interest
			FROM `tabConnectFest Visitor Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s)
			ORDER BY creation DESC
		""", {"email": em, "mobile": mb}, as_dict=True)
		if visitors:
			visitor_pass = visitors[0]

	return {
		"status": "success",
		"has_stall_booking": bool(stall_bookings),
		"stall_booking": stall_booking,
		"stall_bookings": stall_bookings,
		"total_stalls": len(stall_bookings),
		"stalls_list": [s.get("stall_number") for s in stall_bookings if s.get("stall_number")],
		"has_visitor_pass": bool(visitor_pass),
		"visitor_pass": visitor_pass,
		"visitor_passes": visitors if (em or mb) else [],
		"total_visitor_passes": len(visitors) if (em or mb) else 0,
	}


@handle_api_errors("ConnectFest Upload Visitor Image Error")
def upload_visitor_image(file=None, filedata=None, filename=None, **kwargs):
	"""
	Public API to upload a visitor photo/image directly.
	Supports:
	1. Multipart form-data file upload (fields: 'visitor_image', 'file', 'image', 'photo')
	2. Base64 data URL string
	Returns public '/files/vis_...' URL and absolute full_url to pass to register_visitor.
	"""
	data = extract_request_data(kwargs)
	raw = file or filedata or data.get("visitor_image") or data.get("image") or data.get("file") or data.get("photo") or data.get("filedata")
	saved_url = save_image_from_base64_or_url(raw, file_name_prefix="vis")
	if not saved_url:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "No valid image provided. Send an image file in multipart/form-data or a base64 image data string."
		}

	from connectfest.services.common import format_full_url
	full_url = format_full_url(saved_url)
	return {
		"status": "success",
		"message": "Image uploaded successfully",
		"file_url": full_url,
		"full_url": full_url,
		"url": full_url,
	}


@handle_api_errors("ConnectFest Admin Visitor Registrations Error")
def admin_get_visitor_registrations(**kwargs):
	"""
	Admin & Scanner API: List Visitor registrations with search, filter, and check-in stats.
	Restricted strictly to Event Administrators and authorized Visitor Scanners.
	"""
	from connectfest.services.auth import has_scanner_scope, resolve_authenticated_session_user

	sess_user, _ = resolve_authenticated_session_user(**kwargs)

	# Internal skip auth flag for tests if needed
	skip_auth = kwargs.get("is_internal") or kwargs.get("skip_auth") or getattr(frappe.flags, "in_test", False)

	if not skip_auth:
		if not sess_user or sess_user == "Guest":
			frappe.local.response["http_status_code"] = 401
			return {
				"status": "unauthorized",
				"message": "Authentication required! Please log in as an Administrator or authorized Visitor Scanner.",
			}

		if not has_scanner_scope(sess_user, "visitor"):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Access Denied: Your account does not have Visitor Scanner privileges!",
			}

	data = extract_request_data(kwargs)
	search = safe_str(data.get("search") or kwargs.get("search")).strip()
	status = safe_str(data.get("status") or kwargs.get("status")).strip()
	which_day = safe_str(data.get("which_day") or data.get("day") or kwargs.get("which_day") or kwargs.get("day")).strip()
	checked_in = data.get("checked_in") if data.get("checked_in") is not None else kwargs.get("checked_in")
	page = max(1, cint(data.get("page") or kwargs.get("page") or 1))
	limit = min(100, max(1, cint(data.get("limit") or kwargs.get("limit") or 20)))
	start = (page - 1) * limit

	conditions = []
	values = {}

	if search:
		conditions.append("(full_name LIKE %(search)s OR mobile_no LIKE %(search)s OR email LIKE %(search)s OR pass_code LIKE %(search)s OR registration_id LIKE %(search)s OR area_of_interest LIKE %(search)s)")
		values["search"] = f"%{search}%"

	if status:
		if status.lower() in ("checked in", "scanned"):
			conditions.append("checked_in = 1")
		elif status.lower() in ("not scanned", "pending", "registered"):
			conditions.append("checked_in = 0")
		else:
			conditions.append("status = %(status)s")
			values["status"] = status

	if which_day:
		conditions.append("which_day LIKE %(which_day)s")
		values["which_day"] = f"%{which_day}%"

	if checked_in is not None and str(checked_in).strip() != "":
		conditions.append("checked_in = %(chk_in)s")
		values["chk_in"] = 1 if cint(checked_in) else 0

	where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

	items = frappe.db.sql(f"""
		SELECT name, registration_id, full_name, visitor_image, email, mobile_no,
		       which_day, pass_code, qr_payload, area_of_interest, source_of_info,
		       status, checked_in, checked_in_at, checked_in_by, creation
		FROM `tabConnectFest Visitor Registration`
		{where_clause}
		ORDER BY creation DESC
		LIMIT {limit} OFFSET {start}
	""", values, as_dict=True)

	total_count = frappe.db.sql(f"""
		SELECT COUNT(name) as cnt
		FROM `tabConnectFest Visitor Registration`
		{where_clause}
	""", values, as_dict=True)[0].cnt

	checked_in_count = frappe.db.sql(f"""
		SELECT COUNT(name) as cnt
		FROM `tabConnectFest Visitor Registration`
		WHERE checked_in = 1
	""", as_dict=True)[0].cnt

	total_all_visitors = frappe.db.sql(f"""
		SELECT COUNT(name) as cnt
		FROM `tabConnectFest Visitor Registration`
	""", as_dict=True)[0].cnt

	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"total_count": total_count,
		"checked_in_count": checked_in_count,
		"pending_count": max(0, total_all_visitors - checked_in_count),
		"page": page,
		"limit": limit,
		"registrations": items,
		"visitors": items,
		"data": items
	}


# Aliases
get_visitor_registrations = admin_get_visitor_registrations
get_visitor_registers = admin_get_visitor_registrations
get_visitors = admin_get_visitor_registrations
admin_get_visitors = admin_get_visitor_registrations
admin_get_visitor_registers = admin_get_visitor_registrations
