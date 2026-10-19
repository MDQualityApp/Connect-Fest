import json
import time
import frappe
from frappe.utils import now_datetime, flt
from connectfest.services.common import (
	STALL_QUEUE_KEY,
	VISITOR_QUEUE_KEY,
	STALL_CACHE_PREFIX,
	VISITOR_CACHE_PREFIX,
	VALID_SOURCES,
	extract_request_data,
	parse_boolean,
	get_cached_record,
	set_cached_record,
	generate_stall_id,
	generate_visitor_id,
	save_image_from_base64_or_url,
	handle_api_errors,
)
from connectfest.services.pricing import get_tier_pricing, STALL_PRICING
from connectfest.services.auth import ensure_frappe_user
from connectfest.services.queue import trigger_async_flush


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

	full_name = (data.get("full_name") or data.get("name") or "").strip()
	company_name = (data.get("company_name") or "").strip()
	mobile_no = (data.get("mobile_no") or data.get("phone") or "").strip()
	email = (data.get("email") or "").strip()
	raw_logo = (data.get("company_logo") or data.get("logo") or "").strip()
	company_logo = save_image_from_base64_or_url(raw_logo, file_name_prefix=f"logo_{mobile_no[-4:] if len(mobile_no)>=4 else 'stall'}")
	location = (data.get("location") or "").strip()
	is_bni_member = parse_boolean(data.get("is_bni_member", 0))

	region_name = (data.get("region_name") or "").strip()
	chapter_name = (data.get("chapter_name") or "").strip()
	industry = (data.get("industry") or "").strip()
	segment = (data.get("segment") or "").strip()

	raw_category = (data.get("stall_category") or data.get("category") or data.get("tier") or "").strip().capitalize()
	source_of_info = (data.get("source_of_info") or data.get("source") or "").strip()
	stall_number = (data.get("stall_number") or data.get("stall_no") or "").strip()
	sync_mode = parse_boolean(data.get("sync", 0))
	create_order = parse_boolean(data.get("create_order", 0))

	# Validations
	errors = []
	if not full_name:
		errors.append("Name (full_name) is required")
	if not company_name:
		errors.append("Company Name (company_name) is required")
	if not mobile_no:
		errors.append("Mobile Number (mobile_no) is required")
	elif len("".join(filter(str.isdigit, mobile_no))) != 10:
		errors.append("Valid 10-digit Mobile Number is required")
	if not email:
		errors.append("Email Address (email) is required")
	elif "@" not in email:
		errors.append("Valid Email Address is required")
	if not source_of_info:
		errors.append("How did you know about this event? (source_of_info) is required")

	# Enforce Strict Uniqueness on Email and Mobile
	if email or mobile_no:
		# 1. Check existing exhibitor registrations
		existing = frappe.db.sql("""
			SELECT name, registration_id, full_name, company_name, email, mobile_no,
			       stall_number, stall_category, amount, payment_status, status
			FROM `tabConnectFest Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s)
			ORDER BY creation DESC LIMIT 1
		""", {"email": email, "mobile": mobile_no}, as_dict=True)
		if existing:
			ex = existing[0]
			ex_email = (ex.get("email") or "").strip().lower()
			ex_mobile = (ex.get("mobile_no") or "").strip()
			req_email = (email or "").strip().lower()
			req_mobile = (mobile_no or "").strip()

			# Conflict A: Mobile number already registered to a DIFFERENT email!
			if req_mobile and ex_mobile == req_mobile and ex_email != req_email:
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": f"Mobile number '{req_mobile}' is already registered with another account ({ex_email})! Each mobile number can only be used once."
				}

			# Conflict B: Email already registered with a DIFFERENT mobile number!
			if req_email and ex_email == req_email and req_mobile and ex_mobile != req_mobile:
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": f"Email address '{req_email}' is already registered with mobile number {ex_mobile}! Please use your registered mobile number."
				}

			# Both match: Exact same attendee returning
			if ex.payment_status == "Paid":
				st_name = ex.stall_number or ex.stall_category
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": f"Each exhibitor can book only 1 stall! You already have an active booked stall: {st_name} (Registration ID: {ex.registration_id})."
				}
			else:
				# Return existing pending registration without creating duplicate!
				ensure_frappe_user(ex_email, ex.full_name or full_name, ex_mobile, role="Website User")
				if company_logo and not ex.get("company_logo"):
					frappe.db.set_value("ConnectFest Registration", ex.name, "company_logo", company_logo)
					frappe.db.commit()
					ex["company_logo"] = company_logo
				return {
					"status": "already_registered",
					"message": f"An exhibitor registration already exists with this email and mobile number! (Registration ID: {ex.registration_id}). Please proceed to select your stall and complete payment.",
					"registration_id": ex.registration_id,
					"data": ex,
				}

		# 2. Check if already registered in Visitor Registration with this mobile or email
		vis = frappe.db.sql("""
			SELECT registration_id, email, mobile_no
			FROM `tabConnectFest Visitor Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s)
			LIMIT 1
		""", {"email": email, "mobile": mobile_no}, as_dict=True)
		if vis:
			v = vis[0]
			v_email = (v.get("email") or "").strip().lower()
			v_mobile = (v.get("mobile_no") or "").strip()
			if mobile_no and v_mobile == mobile_no and v_email != email.lower():
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": f"Mobile number '{mobile_no}' is already registered as a Visitor under {v_email}! Each mobile number can only be used once."
				}
			if email.lower() == v_email and mobile_no and v_mobile != mobile_no:
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": f"Email '{email}' is already registered as a Visitor with mobile number {v_mobile}! Please use your registered mobile number."
				}

			# Exact match or conflict in Visitor Registration - strictly block duplicate role
			field_matched = "email and mobile number" if (email.lower() == v_email and mobile_no == v_mobile) else ("email" if email.lower() == v_email else "mobile number")
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"This {field_matched} is already registered as a Visitor! (Pass ID: {v.registration_id}). Each attendee can only be an Exhibitor or a Visitor, not both."
			}

		# 3. Check if mobile_no or phone is already bound to another User in Frappe
		if mobile_no:
			user_with_mob = frappe.db.get_value("User", {"mobile_no": mobile_no}, "name") or frappe.db.get_value("User", {"phone": mobile_no}, "name")
			if user_with_mob and user_with_mob.lower() != email.lower():
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": f"Mobile number '{mobile_no}' is already registered with another account ({user_with_mob})! Each mobile number can only be used once."
				}

	# Stall Specific Validation (if stall_number or category is provided)
	dynamic_prices = get_tier_pricing()
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
		if raw_category not in STALL_PRICING:
			valid_tiers = ", ".join(STALL_PRICING.keys())
			errors.append(f"Invalid stall_category '{raw_category}'. Allowed: {valid_tiers}")
		stall_category = raw_category
		amount = dynamic_prices.get(stall_category, 0.0)
	else:
		# Initial registration without choosing a stall yet
		stall_category = ""
		amount = 0.0

	if is_bni_member:
		if not region_name:
			errors.append("Region Name (region_name) is required for BNI members")
		if not chapter_name:
			errors.append("Chapter Name (chapter_name) is required for BNI members")

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

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

	record = {
		"name": reg_id,
		"registration_id": reg_id,
		"full_name": full_name,
		"company_name": company_name,
		"company_logo": company_logo,
		"location": location,
		"mobile_no": mobile_no,
		"email": email,
		"is_bni_member": is_bni_member,
		"region_name": region_name if is_bni_member else "",
		"chapter_name": chapter_name if is_bni_member else "",
		"industry": industry,
		"segment": segment,
		"source_of_info": matched_source,
		"stall_number": stall_number or None,
		"stall_category": stall_category or None,
		"amount": amount,
		"payment_status": "Pending",
		"razorpay_order_id": "",
		"razorpay_payment_id": "",
		"razorpay_signature": "",
		"payment_date": None,
		"status": "Registered",
		"creation": now_str,
		"modified": now_str,
		"modified_by": "Administrator",
		"owner": "Administrator",
		"docstatus": 0,
		"idx": 0,
	}

	# Ensure Frappe User account exists
	ensure_frappe_user(email, full_name, mobile_no, role="Website User")
	if company_logo:
		try:
			frappe.db.set_value("User", email, "user_image", company_logo)
		except Exception:
			pass

	# Direct Reliable Insert into Database & Redis Cache
	try:
		doc = frappe.get_doc({"doctype": "ConnectFest Registration", **record})
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()

		actual_reg_id = doc.registration_id or doc.name
		record["name"] = doc.name
		record["registration_id"] = actual_reg_id
		set_cached_record(cache, f"{STALL_CACHE_PREFIX}{actual_reg_id}", record)
		if doc.name != actual_reg_id:
			set_cached_record(cache, f"{STALL_CACHE_PREFIX}{doc.name}", record)

		response_data = {
			"status": "success",
			"type": "stall",
			"message": "Exhibitor registration successful! Please proceed to select stall.",
			"registration_id": actual_reg_id,
			"data": record,
		}

		if create_order and stall_number:
			from connectfest.services.payments import create_razorpay_order
			order_info = create_razorpay_order(registration_id=actual_reg_id, stall_number=stall_number)
			response_data["razorpay_order"] = order_info.get("order")

		return response_data
	except Exception as e:
		frappe.log_error(title="ConnectFest Stall Insert Error", message=frappe.get_traceback())
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}


@handle_api_errors("ConnectFest Select Stall Error")
def select_stall(registration_id=None, stall_number=None, **kwargs):
	"""
	Allows a registered exhibitor to explicitly choose or change their stall before payment.
	Validates availability, updates registration record, applies dynamic pricing, and places a 10-minute hold.
	"""
	from connectfest.services.stalls import hold_stall
	data = extract_request_data(kwargs)
	reg_id = (registration_id or data.get("registration_id") or "").strip()
	stall_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip()

	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Login required to select stall. Please log in using your registered mobile number or email via OTP.",
		}

	# Auto-resolve registration_id for logged-in exhibitor if not explicitly provided
	if not reg_id:
		reg_id = frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "registration_id") or frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "name")

	# Check visitor restriction
	if (reg_id and str(reg_id).startswith("VIS-")) or frappe.db.exists("ConnectFest Visitor Registration", {"registration_id": reg_id}) or frappe.db.exists("ConnectFest Visitor Registration", reg_id):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": "Visitors are registered for Free Entry passes only and cannot select or purchase exhibitor stalls. To book a stall, please register as an Exhibitor.",
		}

	if not reg_id:
		if frappe.db.exists("ConnectFest Visitor Registration", {"email": sess_user}):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Visitors are registered for Free Entry passes only and cannot select or purchase exhibitor stalls. To book a stall, please register as an Exhibitor.",
			}
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	if not stall_num:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Stall Number (stall_number) is mandatory! Please select a valid stall (e.g. PR-05, ST-12, SL-01)."}

	if not frappe.db.exists("ConnectFest Stall", stall_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{stall_num}' does not exist in ConnectFest inventory"}

	stall_doc = frappe.get_doc("ConnectFest Stall", stall_num)
	if stall_doc.is_blocked_by_admin or stall_doc.status == "Blocked":
		frappe.local.response["http_status_code"] = 403
		return {"status": "error", "message": f"Stall '{stall_num}' is blocked by administration ({stall_doc.blocked_reason or 'Reserved'})"}

	if stall_doc.status == "Booked" and stall_doc.booked_by_registration != reg_id:
		frappe.local.response["http_status_code"] = 409
		return {"status": "error", "message": f"Stall '{stall_num}' is already booked and confirmed by another exhibitor"}

	cache = frappe.cache()
	record = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}")
	if not record and frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
		records = frappe.get_all("ConnectFest Registration", filters={"registration_id": reg_id}, fields=["*"], limit=1)
		if records:
			record = records[0]

	if not record:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Registration '{reg_id}' not found"}

	# IDOR Prevention: Ensure logged-in user owns this registration (unless Admin)
	from connectfest.services.auth import is_admin_user
	if not is_admin_user(sess_user):
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
		cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{old_stall}"))
		if frappe.db.exists("ConnectFest Stall", old_stall):
			old_doc = frappe.get_doc("ConnectFest Stall", old_stall)
			if old_doc.status == "Hold":
				old_doc.status = "Available"
				old_doc.hold_token = None
				old_doc.hold_expires_at = None
				old_doc.save(ignore_permissions=True)
				frappe.db.commit()

	dynamic_prices = get_tier_pricing()
	tier = stall_doc.tier
	price = dynamic_prices.get(tier, stall_doc.price)

	record["stall_number"] = stall_num
	record["stall_category"] = tier
	record["amount"] = price
	set_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}", record)

	db_target = None
	if frappe.db.exists("ConnectFest Registration", reg_id):
		db_target = reg_id
	elif frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
		db_target = frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "name")

	if db_target:
		frappe.db.set_value(
			"ConnectFest Registration",
			db_target,
			{
				"stall_number": stall_num,
				"stall_category": tier,
				"amount": price,
			},
			update_modified=False,
		)
		frappe.db.commit()

	# Hold stall for 10 mins
	hold_res = hold_stall(stall_number=stall_num, email=record.get("email"), mobile_no=record.get("mobile_no"), full_name=record.get("full_name"), company_name=record.get("company_name"))

	return {
		"status": "success",
		"message": f"Stall {stall_num} ({tier}) selected successfully for {reg_id}",
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

	full_name = (data.get("full_name") or data.get("name") or "").strip()
	email = (data.get("email") or data.get("email_address") or "").strip().lower()
	mobile_no = (data.get("mobile_no") or data.get("mobile") or data.get("phone") or "").strip()
	which_day = (data.get("which_day") or data.get("day") or "Both days").strip()
	source_of_info = (data.get("source_of_info") or data.get("source") or "").strip()

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

	# Check for Existing Registration (Strict Uniqueness on Email & Mobile)
	existing = frappe.db.sql("""
		SELECT name, registration_id, full_name, email, mobile_no, which_day, pass_code, qr_payload, status, checked_in, checked_in_at
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
				"message": f"Mobile number '{mobile_no}' is already registered with another visitor ({rec_email})! Each mobile number can only be used once."
			}

		# Conflict B: Email matches, but mobile is different!
		if rec_email == email.lower() and mobile_no and rec_mobile != mobile_no:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"Email address '{email}' is already registered with mobile number {rec_mobile}! Please use your registered mobile number."
			}

		ensure_frappe_user(rec_email, rec.full_name or full_name, rec_mobile, role="Website User")
		return {
			"status": "already_registered",
			"message": "You are already registered for ConnectFest 2027! Here is your entrance pass.",
			"registration_id": rec.registration_id,
			"data": rec,
			"pass": {
				"pass_code": rec.pass_code or f"CF27-{rec.name[-6:]}",
				"full_name": rec.full_name,
				"email": rec.email,
				"mobile_no": rec.mobile_no,
				"which_day": rec.which_day or "Both days",
				"access": "BOTH DAYS" if (rec.which_day or "Both days") == "Both days" else rec.which_day.upper(),
				"venue": "Chennai Trade Centre, Nandambakkam",
				"dates": "20 & 21 Feb 2027",
				"qr_payload": rec.qr_payload or f"CONNECTFEST2027:{rec.pass_code}:{rec.email}",
				"status": rec.status,
				"checked_in": rec.checked_in,
			},
		}

	# Check if already registered as an exhibitor with this email or mobile
	exhibitor = frappe.db.sql("""
		SELECT registration_id, email, mobile_no
		FROM `tabConnectFest Registration`
		WHERE email = %(email)s OR mobile_no = %(mobile)s
		LIMIT 1
	""", {"email": email, "mobile": mobile_no}, as_dict=True)
	if exhibitor:
		ex = exhibitor[0]
		ex_email = (ex.get("email") or "").strip().lower()
		ex_mobile = (ex.get("mobile_no") or "").strip()
		if mobile_no and ex_mobile == mobile_no and ex_email != email.lower():
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"Mobile number '{mobile_no}' is already registered with an Exhibitor ({ex_email})! Each mobile number can only be used once."
			}
		field_matched = "email" if ex_email == email.lower() else "mobile number"
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": f"This {field_matched} is already registered as an Exhibitor! (Registration ID: {ex.registration_id}). Exhibitors get automated badge access.",
		}

	# Check if mobile_no is bound to another active User
	if mobile_no:
		user_with_mob = frappe.db.get_value("User", {"mobile_no": mobile_no}, "name") or frappe.db.get_value("User", {"phone": mobile_no}, "name")
		if user_with_mob and user_with_mob.lower() != email.lower():
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"Mobile number '{mobile_no}' is already registered with another account ({user_with_mob})! Each mobile number can only be used once.",
			}

	cache = frappe.cache()
	reg_id = generate_visitor_id(cache)
	while True:
		try:
			seq = cache.incr(cache.make_key("connectfest:visitor_pass_counter"))
		except Exception:
			seq = int(time.time() * 1000) % 1000000
		pass_code = f"CF27-{seq:06d}"
		if not frappe.db.exists("ConnectFest Visitor Registration", {"pass_code": pass_code}):
			break
	qr_payload = f"CONNECTFEST2027:{pass_code}:{email}:{mobile_no}"
	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S.%f")

	record = {
		"name": reg_id,
		"registration_id": reg_id,
		"full_name": full_name,
		"email": email,
		"mobile_no": mobile_no,
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

	pass_info = {
		"pass_code": pass_code,
		"full_name": full_name,
		"email": email,
		"mobile_no": mobile_no,
		"which_day": which_day,
		"access": "BOTH DAYS" if which_day == "Both days" else which_day.upper(),
		"venue": "Chennai Trade Centre, Nandambakkam",
		"dates": "20 & 21 Feb 2027",
		"qr_payload": qr_payload,
		"status": "Registered",
		"checked_in": 0,
	}

	try:
		doc = frappe.get_doc({"doctype": "ConnectFest Visitor Registration", **record})
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		set_cached_record(cache, f"{VISITOR_CACHE_PREFIX}{reg_id}", record)
		ensure_frappe_user(email, full_name, mobile_no, role="Website User")
		return {
			"status": "success",
			"type": "visitor",
			"message": "Visitor registration successful! Your entrance pass is ready.",
			"registration_id": reg_id,
			"data": record,
			"pass": pass_info,
		}
	except Exception as e:
		frappe.log_error(title="ConnectFest Visitor Insert Error", message=frappe.get_traceback())
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}


@handle_api_errors("ConnectFest Check Booking Status Error")
def check_user_booking_status(email=None, mobile_no=None, **kwargs):
	"""Check if an email or mobile has an existing stall booking or visitor pass."""
	data = extract_request_data(kwargs)
	em = (email or data.get("email") or "").strip().lower()
	mb = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()

	stall_booking = None
	visitor_pass = None

	if em or mb:
		stalls = frappe.db.sql("""
			SELECT registration_id, full_name, company_name, email, mobile_no, stall_number, stall_category, amount, payment_status
			FROM `tabConnectFest Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s) AND payment_status = 'Paid'
			LIMIT 1
		""", {"email": em, "mobile": mb}, as_dict=True)
		if stalls:
			stall_booking = stalls[0]

		visitors = frappe.db.sql("""
			SELECT registration_id, full_name, email, mobile_no, which_day, pass_code, status, checked_in
			FROM `tabConnectFest Visitor Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s)
			LIMIT 1
		""", {"email": em, "mobile": mb}, as_dict=True)
		if visitors:
			visitor_pass = visitors[0]

	return {
		"status": "success",
		"has_stall_booking": bool(stall_booking),
		"stall_booking": stall_booking,
		"has_visitor_pass": bool(visitor_pass),
		"visitor_pass": visitor_pass,
	}
