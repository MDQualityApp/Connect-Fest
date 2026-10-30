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
	bill = (data.get("bill") or data.get("billing_name") or data.get("bill_to") or "").strip()
	gst = (data.get("gst") or data.get("gst_number") or data.get("gstin") or "").strip()
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
					"message": f"Mobile number '{req_mobile}' is already registered with another account ({ex_email})! Each mobile number can only be used once."
				}

			# Conflict B: Email already registered with a DIFFERENT mobile number!
			if req_email and ex_email == req_email and req_mobile and ex_mobile != req_mobile:
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": f"Email address '{req_email}' is already registered with mobile number {ex_mobile}! Please use your registered mobile number."
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
						"message": f"You have already booked and confirmed stall '{stall_number}' (Registration ID: {already_booked_this[0].get('registration_id')})."
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
						"message": f"You already have a pending registration for stall '{stall_number}'! (Registration ID: {p_rec.registration_id}). Please proceed to complete payment.",
						"registration_id": p_rec.registration_id,
						"data": p_rec,
					}
			else:
				# No stall_number specified:
				# If there is an existing pending registration without stall chosen, reuse it
				# unless caller explicitly requested a new registration (allow_new / new_booking)
				allow_new = parse_boolean(data.get("allow_new", 0)) or parse_boolean(data.get("new_booking", 0))
				if not allow_new:
					pending_no_stall = [
						r for r in all_ex
						if r.get("payment_status") != "Paid" and not r.get("stall_number")
					]
					if pending_no_stall:
						p_rec = pending_no_stall[0]
						ensure_frappe_user(ex_email, p_rec.full_name or full_name, ex_mobile, role="Website User")
						return {
							"status": "already_registered",
							"message": f"An exhibitor registration already exists with this email and mobile number! (Registration ID: {p_rec.registration_id}). Please proceed to select your stall and complete payment.",
							"registration_id": p_rec.registration_id,
							"data": p_rec,
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

	raw_pay_mode = (data.get("payment_mode") or "").strip().capitalize()
	if raw_pay_mode.lower() in ("cash", "cheque", "bank transfer"):
		from connectfest.services.auth import is_admin_user
		sess_user = getattr(frappe.session, "user", None)
		if not is_admin_user(sess_user):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Cash payments can only be processed and recorded by Event Administration at the venue desk. Exhibitors must pay online via Razorpay."
			}

	has_stall_selected = bool(stall_number or stall_category)

	record = {
		"name": reg_id,
		"registration_id": reg_id,
		"full_name": full_name,
		"company_name": company_name,
		"company_logo": company_logo,
		"location": location,
		"bill": bill,
		"gst": gst,
		"mobile_no": mobile_no,
		"email": email,
		"is_bni_member": is_bni_member,
		"region_name": region_name if is_bni_member else "",
		"chapter_name": chapter_name if is_bni_member else "",
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
		"creation": now_str,
		"modified": now_str,
		"modified_by": "Administrator",
		"owner": "Administrator",
		"docstatus": 0,
		"idx": 0,
	}

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

		# Provision Frappe User account immediately (Synchronous - zero background worker/cron dependency)
		try:
			ensure_frappe_user(email, full_name, mobile_no, role="Website User")
			if company_logo:
				frappe.db.set_value("User", email, "user_image", company_logo)
		except Exception:
			pass

		response_data = {
			"status": "success",
			"type": "stall",
			"message": "Exhibitor registration successful! Please proceed to select stall.",
			"registration_id": actual_reg_id,
			"full_name": full_name,
			"data": record,
		}

		if create_order and stall_number:
			from connectfest.services.payments import create_razorpay_order
			order_info = create_razorpay_order(registration_id=actual_reg_id, stall_number=stall_number, is_internal=True)
			response_data["razorpay_order"] = order_info.get("order")

		return response_data
	except Exception as e:
		frappe.log_error(title="ConnectFest Stall Insert Error", message=frappe.get_traceback())
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}


@handle_api_errors("ConnectFest Select Stall Error")
def select_stall(registration_id=None, stall_number=None, **kwargs):
	"""
	Allows a registered exhibitor to explicitly choose or change their stall before payment.
	Validates availability, updates registration record, applies dynamic pricing, and places a 10-minute hold.
	"""
	from connectfest.services.stalls import hold_stall
	cache = frappe.cache()
	data = extract_request_data(kwargs)
	reg_id = (registration_id or data.get("registration_id") or "").strip()
	stall_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip()

	sess_user = getattr(frappe.session, "user", None)
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
	from connectfest.services.auth import is_admin_user
	is_admin = is_admin_user(sess_user)
	if (stall_doc.is_blocked_by_admin or stall_doc.status == "Blocked") and not is_admin:
		frappe.local.response["http_status_code"] = 403
		return {"status": "error", "message": f"Stall '{stall_num}' is blocked by administration ({stall_doc.blocked_reason or 'Reserved'})"}

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

	# Check visitor restriction BEFORE creating or auto-resolving registrations
	if (reg_id and str(reg_id).startswith("VIS-")) or (reg_id and (frappe.db.exists("ConnectFest Visitor Registration", {"registration_id": reg_id}) or frappe.db.exists("ConnectFest Visitor Registration", reg_id))):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": "Visitors are registered for Free Entry passes only and cannot select or purchase exhibitor stalls. To book a stall, please register as an Exhibitor.",
		}

	# Identify candidate email and mobile
	user_email = (data.get("email") or (sess_user if sess_user and sess_user != "Guest" else "")).strip().lower()
	user_mobile = (data.get("mobile_no") or data.get("phone") or (frappe.db.get_value("User", sess_user, "mobile_no") if sess_user and sess_user != "Guest" else "") or (frappe.db.get_value("User", sess_user, "phone") if sess_user and sess_user != "Guest" else "") or "").strip()

	# If the user is registered as a visitor by email or mobile, forbid stall selection
	if not reg_id:
		is_vis = False
		if user_email and frappe.db.exists("ConnectFest Visitor Registration", {"email": user_email}):
			is_vis = True
		elif user_mobile and frappe.db.exists("ConnectFest Visitor Registration", {"mobile_no": user_mobile}):
			is_vis = True
		if is_vis:
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Visitors are registered for Free Entry passes only and cannot select or purchase exhibitor stalls. To book a stall, please register as an Exhibitor.",
			}

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

				company_name = (data.get("company_name") or prev.get("company_name") or "").strip()
				if not company_name:
					frappe.local.response["http_status_code"] = 400
					return {
						"status": "error",
						"message": "Company Name (company_name) is required. Please complete exhibitor registration before selecting a stall, or provide 'registration_id'.",
					}

				new_reg_id = generate_stall_id(cache)
				dynamic_prices = get_tier_pricing()
				tier = stall_doc.tier
				price = dynamic_prices.get(tier, stall_doc.price)

				cand_name = (
					data.get("full_name")
					or prev.get("full_name")
					or (frappe.db.get_value("User", sess_user, "full_name") if sess_user and sess_user != "Guest" else None)
					or ""
				).strip()
				if cand_name and "@" not in cand_name:
					full_name = cand_name
				else:
					full_name = (user_email or sess_user or "Exhibitor").split("@")[0].replace(".", " ").replace("_", " ").title()

				raw_source = data.get("source_of_info") or prev.get("source_of_info") or "Website"
				matched_source = raw_source if raw_source in VALID_SOURCES else "Other"

				is_bni = parse_boolean(data.get("is_bni_member", prev.get("is_bni_member") or 0))
				region_name = (data.get("region_name") or prev.get("region_name") or "").strip()
				chapter_name = (data.get("chapter_name") or prev.get("chapter_name") or "").strip()
				if is_bni and not (region_name and chapter_name):
					is_bni = 0

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

	dynamic_prices = get_tier_pricing()
	tier = stall_doc.tier
	price = dynamic_prices.get(tier, stall_doc.price)

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
		frappe.db.set_value(
			"ConnectFest Registration",
			db_target,
			{
				"stall_number": stall_num,
				"stall_category": tier,
				"amount": price,
				"payment_status": "Pending",
				"payment_mode": "Online",
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
	raw_image = (data.get("visitor_image") or data.get("image") or data.get("photo") or data.get("visitor_photo") or data.get("avatar") or "").strip()
	visitor_image = save_image_from_base64_or_url(raw_image, file_name_prefix=f"vis_{mobile_no[-4:] if len(mobile_no)>=4 else 'pass'}") if raw_image else ""
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

	cache = frappe.cache()
	lock_email_key = cache.make_key(f"connectfest:lock_vreg:{email}") if email else None
	lock_mob_key = cache.make_key(f"connectfest:lock_vreg:{mobile_no}") if mobile_no else None
	has_lock = False
	if lock_email_key or lock_mob_key:
		try:
			acq_e = cache.set(lock_email_key, "1", nx=True, ex=10) if lock_email_key else True
			acq_m = cache.set(lock_mob_key, "1", nx=True, ex=10) if lock_mob_key else True
			if not (acq_e and acq_m):
				# Another concurrent thread is currently creating this registration!
				time.sleep(0.05)
			else:
				has_lock = True
		except Exception:
			has_lock = True

	# Check for Existing Registration (Strict Uniqueness on Email & Mobile)
	existing = frappe.db.sql("""
		SELECT name, registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code, qr_payload, status, checked_in, checked_in_at, area_of_interest
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

		if visitor_image and not rec.get("visitor_image"):
			try:
				frappe.db.set_value("ConnectFest Visitor Registration", rec.name, "visitor_image", visitor_image)
				rec["visitor_image"] = visitor_image
				frappe.db.commit()
			except Exception:
				pass

		ensure_frappe_user(rec_email, rec.full_name or full_name, rec_mobile, role="Website User")
		if visitor_image:
			try:
				frappe.db.set_value("User", rec_email, "user_image", visitor_image)
			except Exception:
				pass

		return {
			"status": "already_registered",
			"message": "You are already registered for ConnectFest 2027! Here is your entrance pass.",
			"registration_id": rec.registration_id,
			"data": rec,
			"pass": {
				"pass_code": rec.pass_code or f"CF27-{rec.name[-6:]}",
				"full_name": rec.full_name,
				"visitor_image": rec.get("visitor_image") or visitor_image or "",
				"email": rec.email,
				"mobile_no": rec.mobile_no,
				"which_day": rec.which_day or "Both days",
				"access": "BOTH DAYS" if (rec.which_day or "Both days") == "Both days" else rec.which_day.upper(),
				"area_of_interest": rec.area_of_interest or "",
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
		"visitor_image": visitor_image,
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

	pass_info = {
		"pass_code": pass_code,
		"full_name": full_name,
		"visitor_image": visitor_image,
		"email": email,
		"mobile_no": mobile_no,
		"area_of_interest": area_of_interest,
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
		doc.flags.ignore_validate = True
		doc.flags.ignore_links = True
		doc.insert()
		frappe.db.commit()
		set_cached_record(cache, f"{VISITOR_CACHE_PREFIX}{reg_id}", record)

		# Provision Frappe User account immediately
		try:
			ensure_frappe_user(email, full_name, mobile_no, role="Website User")
		except Exception:
			pass

		if visitor_image:
			try:
				frappe.db.set_value("User", email, "user_image", visitor_image)
			except Exception:
				pass

		return {
			"status": "success",
			"type": "visitor",
			"message": "Visitor registration successful! Your entrance pass is ready.",
			"registration_id": reg_id,
			"pass_code": pass_code,
			"full_name": full_name,
			"data": record,
			"pass": pass_info,
		}
	except Exception as e:
		frappe.log_error(title="ConnectFest Visitor Insert Error", message=frappe.get_traceback())
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}
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
	"""Check if an email or mobile has an existing stall booking or visitor pass."""
	data = extract_request_data(kwargs)
	em = (email or data.get("email") or "").strip().lower()
	mb = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()

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
			SELECT registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code, status, checked_in, checked_in_at, source_of_info, area_of_interest
			FROM `tabConnectFest Visitor Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s)
			ORDER BY creation DESC LIMIT 1
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

	from connectfest.services.common import get_base_url
	base = get_base_url()
	full_url = f"{base}{saved_url}" if saved_url.startswith("/") else saved_url
	return {
		"status": "success",
		"message": "Image uploaded successfully",
		"file_url": saved_url,
		"full_url": full_url,
	}
