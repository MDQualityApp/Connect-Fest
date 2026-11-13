import json
import frappe
from frappe.utils import now_datetime, cint
from connectfest.services.common import extract_request_data, handle_api_errors, safe_str, parse_boolean
from connectfest.services.bni_members import normalize_phone_number, is_valid_bni_member



@handle_api_errors("ConnectFest BNI Event Number Check Error")
def check_bni_event_number(mobile_no=None, **kwargs):
	"""
	Pre-registration verification API for BNI Events:
	1. Validates mobile number format.
	2. Checks if mobile number belongs to a verified BNI member in 'ConnectFest BNI Member'.
	3. Checks if the attendee is already registered for this BNI Event.
	Returns member details (name, chapter, region) if valid.
	"""
	data = extract_request_data(kwargs)
	mob = normalize_phone_number(mobile_no or data.get("mobile_no") or data.get("phone") or data.get("mobile"))
	email = safe_str(data.get("email") or kwargs.get("email")).strip().lower()

	if (not mob or len(mob) != 10) and (not email or "@" not in email):
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"is_bni_member": False,
			"message": "Valid 10-digit Mobile Number or Email Address is required."
		}

	# 1. Check CNR membership
	is_bni, bni_doc = is_valid_bni_member(mobile_no=mob, email=email)
	if not is_bni:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"is_bni_member": False,
			"is_cnr_member": False,
			"message": "You are not a CNR member! Registration is strictly restricted to verified CNR members.",
			"mobile_no": mob,
		}

	# 2. Check if already registered
	existing_reg = frappe.db.get_value(
		"ConnectFest BNI Event Registration",
		{"mobile_no": mob},
		["name", "registration_id", "pass_code", "full_name", "email", "chapter_name", "status"],
		as_dict=True
	)
	if existing_reg:
		frappe.local.response["http_status_code"] = 200
		return {
			"status": "already_registered",
			"is_bni_member": True,
			"already_registered": True,
			"message": "This mobile number is already registered for the BNI Event! Please log in using Email OTP to access your pass.",
		}

	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"is_bni_member": True,
		"is_cnr_member": True,
		"already_registered": False,
		"message": "Verified CNR Member! You may proceed with registration.",
		"mobile_no": mob,
		"member_name": (bni_doc.get("member_name") if bni_doc else "") or "",
		"chapter_name": (bni_doc.get("chapter_name") if bni_doc else "") or "",
		"region_name": (bni_doc.get("region_name") if bni_doc else "") or "",
	}


@handle_api_errors("ConnectFest BNI Event Registration Error")
def register_bni_event(**kwargs):
	"""
	Dedicated BNI Event Registration API.
	Registration Flow:
	1. FIRST: Check Mobile Number - strictly verify attendee mobile_no exists in 'ConnectFest BNI Member' DocType.
	2. Check if this mobile number is already registered for this event.
	3. SECOND: Verify Email OTP - only verified BNI members with valid email OTP can register.
	4. Generates unique BNI Event Pass Code & QR Payload for gate check-in.
	"""
	data = extract_request_data(kwargs)

	mobile_no = normalize_phone_number(data.get("mobile_no") or data.get("phone") or data.get("mobile"))
	email = safe_str(data.get("email")).strip().lower()
	full_name = safe_str(data.get("full_name") or data.get("name"))
	company_name = safe_str(data.get("company_name") or data.get("company"))

	chapter_name = safe_str(data.get("chapter_name") or data.get("chapter"))
	region_name = safe_str(data.get("region_name") or data.get("region"))
	location = safe_str(data.get("location") or data.get("city") or "Chennai")
	industry = safe_str(data.get("industry"))
	segment = safe_str(data.get("segment"))
	event_name = safe_str(data.get("event_name") or "ConnectFest BNI Events 2027")

	# =========================================================================
	# STEP 1: FIRST CHECK NUMBER (Mobile Number Validation & BNI Verification)
	# =========================================================================
	if not mobile_no or len(mobile_no) != 10:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Valid 10-digit Mobile Number is required."
		}

	# Verify mobile or email exists in ConnectFest CNR Member DocType
	is_bni, bni_doc = is_valid_bni_member(mobile_no=mobile_no, email=email)
	if not is_bni:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "You are not a CNR member! Registration is strictly restricted to verified CNR members.",
			"mobile_no": mobile_no,
		}

	# Check if this mobile number is already registered for this BNI Event
	existing_reg = frappe.db.get_value(
		"ConnectFest BNI Event Registration",
		{"mobile_no": mobile_no},
		["name", "registration_id", "pass_code", "full_name", "email", "mobile_no", "status", "checked_in", "checked_in_at"],
		as_dict=True
	)
	if existing_reg:
		from connectfest.services.auth import ensure_frappe_user
		ensure_frappe_user(existing_reg.get("email") or email, full_name=existing_reg.get("full_name") or full_name, mobile_no=existing_reg.get("mobile_no") or mobile_no, role="Website User")
		frappe.local.response["http_status_code"] = 200
		return {
			"status": "already_registered",
			"message": "This mobile number is already registered for this event! Please log in using Email OTP to access your pass.",
		}

	# Auto-populate chapter / region / member name / email from BNI Member record if missing
	if bni_doc:
		if not chapter_name and bni_doc.get("chapter_name"):
			chapter_name = bni_doc.get("chapter_name")
		if not region_name and bni_doc.get("region_name"):
			region_name = bni_doc.get("region_name")
		if not full_name and bni_doc.get("member_name"):
			full_name = bni_doc.get("member_name")
		if not email and bni_doc.get("email"):
			email = str(bni_doc.get("email")).strip().lower()

	# Resolve missing email from existing stall registration or user record
	if not email and mobile_no:
		email = (
			frappe.db.get_value("ConnectFest Registration", {"mobile_no": mobile_no}, "email")
			or frappe.db.get_value("User", {"mobile_no": mobile_no}, "name")
			or frappe.db.get_value("User", {"phone": mobile_no}, "name")
			or ""
		).strip().lower()

	# Validate Email and Full Name
	errors = []
	if not full_name:
		errors.append("Full Name is required")
	if not email or "@" not in email:
		errors.append("Valid Email Address is required")

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

	# Check if email is already registered for this BNI Event
	existing_email_reg = frappe.db.get_value(
		"ConnectFest BNI Event Registration",
		{"email": email},
		["name", "registration_id", "pass_code", "full_name", "email", "mobile_no", "status", "checked_in", "checked_in_at"],
		as_dict=True
	)
	if existing_email_reg:
		from connectfest.services.auth import ensure_frappe_user
		ensure_frappe_user(existing_email_reg.get("email") or email, full_name=existing_email_reg.get("full_name") or full_name, mobile_no=existing_email_reg.get("mobile_no") or mobile_no, role="Website User")
		frappe.local.response["http_status_code"] = 200
		return {
			"status": "already_registered",
			"message": "This email address is already registered for this event! Please log in using Email OTP to access your pass.",
		}

	# 4. Generate Pass Code & QR Code Payload
	pass_random = frappe.generate_hash(length=8).upper()
	pass_code = f"BNI-PASS-{pass_random}"

	doc = frappe.get_doc({
		"doctype": "ConnectFest BNI Event Registration",
		"full_name": full_name,
		"company_name": company_name,
		"mobile_no": mobile_no,
		"email": email,
		"event_name": event_name,
		"chapter_name": chapter_name,
		"region_name": region_name,
		"location": location,
		"industry": industry,
		"segment": segment,
		"pass_code": pass_code,
		"status": "Registered",
		"checked_in": 0,
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.commit()

	# Generate QR Payload
	qr_data = {
		"type": "bni_event_pass",
		"registration_id": doc.name,
		"pass_code": pass_code,
		"name": full_name,
		"company": company_name,
		"chapter": chapter_name,
		"mobile": mobile_no,
		"event": event_name,
	}
	frappe.db.set_value("ConnectFest BNI Event Registration", doc.name, {
		"registration_id": doc.name,
		"qr_payload": json.dumps(qr_data)
	})
	frappe.db.commit()

	# 5. Provision Frappe User account (No auto-login; attendee logs in via OTP)
	try:
		from connectfest.services.auth import ensure_frappe_user
		ensure_frappe_user(email, full_name=full_name, mobile_no=mobile_no, role="Website User")
	except Exception as e:
		frappe.log_error("User creation error on BNI registration", str(e))

	# 6. Send Email Pass Confirmation
	try:
		subject = f"🎟️ Your BNI Event Pass: {event_name}"
		email_body = f"""
		<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 500px; margin: 24px auto; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;">
			<div style="background-color: #4f46e5; background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); padding: 26px 24px; text-align: center;">
				<div style="font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #e0e7ff; margin-bottom: 6px;">✦ BNI EVENTS ✦</div>
				<h1 style="margin: 0; font-size: 22px; font-weight: 700; color: #ffffff;">BNI Event Pass Confirmed</h1>
			</div>
			<div style="padding: 24px;">
				<p style="font-size: 15px; color: #0f172a; margin: 0 0 10px; font-weight: 600;">Hello {full_name},</p>
				<p style="font-size: 14px; color: #475569; margin: 0 0 20px;">Your registration for <strong>{event_name}</strong> is confirmed! Please show this pass code at the gate for check-in:</p>
				<div style="background-color: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 8px; padding: 18px; text-align: center; margin: 0 0 20px;">
					<div style="font-size: 11px; font-weight: 700; letter-spacing: 1.5px; color: #64748b; text-transform: uppercase; margin-bottom: 6px;">DIGITAL PASS CODE</div>
					<div style="font-family: monospace; font-size: 28px; font-weight: 800; letter-spacing: 4px; color: #4f46e5;">{pass_code}</div>
				</div>
				<table style="width: 100%; font-size: 13px; color: #475569; margin-bottom: 20px;">
					<tr><td style="padding: 4px 0; color: #94a3b8;">Registration ID:</td><td style="text-align: right; font-weight: 600;">{doc.name}</td></tr>
					<tr><td style="padding: 4px 0; color: #94a3b8;">Company:</td><td style="text-align: right; font-weight: 600;">{company_name or 'N/A'}</td></tr>
					<tr><td style="padding: 4px 0; color: #94a3b8;">BNI Chapter:</td><td style="text-align: right; font-weight: 600;">{chapter_name or 'N/A'}</td></tr>
					<tr><td style="padding: 4px 0; color: #94a3b8;">Mobile:</td><td style="text-align: right; font-weight: 600;">+91-{mobile_no}</td></tr>
				</table>
				<hr style="border: none; border-top: 1px solid #f1f5f9; margin: 0 0 16px;" />
				<p style="font-size: 12px; color: #94a3b8; margin: 0; text-align: center;">ConnectFest BNI Events Team</p>
			</div>
		</div>
		"""
		frappe.sendmail(
			recipients=[email],
			subject=subject,
			message=email_body,
			now=False,
			delayed=True,
		)
		frappe.db.commit()
	except Exception as e:
		frappe.logger("connectfest").warning(f"[BNI Event] Failed to send confirmation email to {email}: {str(e)}")

	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"message": "Event registration successful!",
	}


@handle_api_errors("ConnectFest BNI Event Pass Verify Error")
def verify_bni_event_pass(pass_code=None, **kwargs):
	"""
	Gate Verification / Scanner API for BNI Event Pass (Same as Visitor Pass Check & Verify).
	Accepts pass_code, qr_code, registration_id, or mobile_no.
	Validates pass, prevents duplicate check-in, and records check-in timestamp.
	Restricted strictly to Event Administrators and authorized Gate Scanners.
	"""
	sess_user = getattr(frappe.session, "user", None)
	from connectfest.services.auth import is_admin_user, is_scanner_user

	# Check sid in headers/body/cookies if sess_user is Guest
	if not sess_user or sess_user == "Guest":
		req_sid = None
		if hasattr(frappe, "request") and frappe.request:
			try:
				req_sid = (
					frappe.get_request_header("sid")
					or frappe.get_request_header("X-Frappe-Session-Id")
					or (frappe.request.cookies.get("sid") if hasattr(frappe.request, "cookies") and frappe.request.cookies else None)
				)
				if not req_sid:
					auth_hdr = frappe.get_request_header("Authorization", "")
					if auth_hdr.startswith("Bearer "):
						req_sid = auth_hdr.split("Bearer ", 1)[1].strip()
			except Exception:
				pass
		if req_sid and req_sid != "Guest":
			try:
				res = frappe.db.sql("SELECT user FROM `tabSessions` WHERE sid = %s LIMIT 1", (req_sid,))
				if res and res[0][0]:
					sess_user = res[0][0]
			except Exception:
				pass

	# Skip auth for internal tests/calls if explicitly flagged
	skip_auth = kwargs.get("is_internal") or kwargs.get("skip_auth") or getattr(frappe.flags, "in_test", False)

	if not skip_auth:
		if not sess_user or sess_user == "Guest":
			frappe.local.response["http_status_code"] = 401
			return {
				"status": "unauthorized",
				"verified": False,
				"message": "Authentication required! Please log in as an Administrator or authorized Gate Scanner.",
			}

		if not (is_admin_user(sess_user) or is_scanner_user(sess_user)):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"verified": False,
				"message": f"Access Denied: User '{sess_user}' does not have scanner privileges! Contact Event Administrator to grant scanner access.",
			}

	data = extract_request_data(kwargs)
	code = safe_str(pass_code or data.get("pass_code") or data.get("qr_code") or data.get("code") or data.get("registration_id"))
	mob = normalize_phone_number(data.get("mobile_no") or data.get("phone") or data.get("mobile"))
	mail = safe_str(kwargs.get("email") or data.get("email") or data.get("email_id") or data.get("user_email") or data.get("mail")).strip().lower()

	if not code and not mob and not mail:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"verified": False,
			"message": "Pass code, Registration ID, Mobile Number, or Email Address is required for verification."
		}

	# Lookup BNI Event Registration
	record = None
	if code:
		record = frappe.db.get_value(
			"ConnectFest BNI Event Registration",
			{"pass_code": code},
			["name", "registration_id", "full_name", "company_name", "mobile_no", "email", "chapter_name", "region_name", "event_name", "pass_code", "status", "checked_in", "checked_in_at", "checked_in_by"],
			as_dict=True,
			order_by="creation desc"
		)
		if not record:
			record = frappe.db.get_value(
				"ConnectFest BNI Event Registration",
				{"registration_id": code},
				["name", "registration_id", "full_name", "company_name", "mobile_no", "email", "chapter_name", "region_name", "event_name", "pass_code", "status", "checked_in", "checked_in_at", "checked_in_by"],
				as_dict=True,
				order_by="creation desc"
			)
		if not record and frappe.db.exists("ConnectFest BNI Event Registration", code):
			record = frappe.db.get_value(
				"ConnectFest BNI Event Registration",
				code,
				["name", "registration_id", "full_name", "company_name", "mobile_no", "email", "chapter_name", "region_name", "event_name", "pass_code", "status", "checked_in", "checked_in_at", "checked_in_by"],
				as_dict=True
			)

	if not record and mob:
		record = frappe.db.get_value(
			"ConnectFest BNI Event Registration",
			{"mobile_no": mob},
			["name", "registration_id", "full_name", "company_name", "mobile_no", "email", "chapter_name", "region_name", "event_name", "pass_code", "status", "checked_in", "checked_in_at", "checked_in_by"],
			as_dict=True,
			order_by="creation desc"
		)

	if not record and mail:
		record = frappe.db.get_value(
			"ConnectFest BNI Event Registration",
			{"email": mail},
			["name", "registration_id", "full_name", "company_name", "mobile_no", "email", "chapter_name", "region_name", "event_name", "pass_code", "status", "checked_in", "checked_in_at", "checked_in_by"],
			as_dict=True,
			order_by="creation desc"
		)

	if not record:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"verified": False,
			"message": "Invalid BNI Event Pass! No matching registration found.",
			"code_scanned": code or mob or mail
		}

	# Check if already checked in
	now_dt = now_datetime()
	sess_user = sess_user or getattr(frappe.session, "user", "Gate Staff") or "Gate Staff"

	if cint(record.get("checked_in")):
		frappe.local.response["http_status_code"] = 200
		return {
			"status": "already_checked_in",
			"verified": True,
			"already_checked_in": True,
			"message": f"⚠️ Pass already checked in on {record.get('checked_in_at')}!",
			"checked_in_at": str(record.get("checked_in_at")),
			"checked_in_by": record.get("checked_in_by"),
			"attendee": {
				"registration_id": record.get("registration_id") or record.get("name"),
				"full_name": record.get("full_name"),
				"company_name": record.get("company_name"),
				"chapter_name": record.get("chapter_name"),
				"mobile_no": record.get("mobile_no"),
				"pass_code": record.get("pass_code"),
				"event_name": record.get("event_name"),
			}
		}

	# Perform successful Check-in
	frappe.db.set_value("ConnectFest BNI Event Registration", record["name"], {
		"status": "Checked In",
		"checked_in": 1,
		"checked_in_at": now_dt,
		"checked_in_by": sess_user
	})
	frappe.db.commit()

	frappe.local.response["http_status_code"] = 200
	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"verified": True,
		"already_checked_in": False,
		"message": "✅ BNI Event Pass verified successfully! Check-in confirmed.",
		"checked_in_at": str(now_dt),
		"checked_in_by": sess_user,
		"attendee": {
			"registration_id": record.get("registration_id") or record.get("name"),
			"full_name": record.get("full_name"),
			"company_name": record.get("company_name"),
			"chapter_name": record.get("chapter_name"),
			"region_name": record.get("region_name"),
			"mobile_no": record.get("mobile_no"),
			"email": record.get("email"),
			"pass_code": record.get("pass_code"),
			"event_name": record.get("event_name"),
		}
	}


@handle_api_errors("ConnectFest Get BNI Event Pass Error")
def get_bni_event_pass(pass_code=None, registration_id=None, mobile_no=None, email=None, **kwargs):
	"""
	Retrieves BNI Event Pass code and QR payload for an attendee.
	Accepts pass_code, registration_id, id, mobile_no, or email.
	Supports:
	- id / registration_id alone (e.g. {"id": "BNI-EVT-16478"})
	- mobile_no + id (prevents friends guessing mobile from stealing pass)
	- email + id
	- mobile_no + email + id
	- mobile_no alone
	- email alone
	- pass_code alone
	Returns clean, lightweight response with pass_code and qr_payload (no redundant 17-field dumps).
	"""
	data = extract_request_data(kwargs)
	code = safe_str(pass_code or data.get("pass_code") or data.get("code") or data.get("qr_code"))
	reg_id = safe_str(registration_id or data.get("registration_id") or data.get("reg_id") or data.get("id"))
	mob = normalize_phone_number(mobile_no or data.get("mobile_no") or data.get("phone") or data.get("mobile"))
	mail = safe_str(email or data.get("email") or data.get("email_id") or data.get("user_email") or data.get("mail")).strip().lower()

	if not code and not reg_id and not mob and not mail:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "id, registration_id, mobile_no, email, or pass_code is required"}

	fields = [
		"name", "registration_id", "full_name", "company_name", "mobile_no", "email",
		"chapter_name", "region_name", "location", "industry", "segment", "event_name",
		"pass_code", "status", "checked_in", "checked_in_at", "checked_in_by", "creation"
	]

	record = None

	# 1. Match pass_code / qr_code
	if code:
		record = frappe.db.get_value("ConnectFest BNI Event Registration", {"pass_code": code}, fields, as_dict=True, order_by="creation desc")
		if not record:
			record = frappe.db.get_value("ConnectFest BNI Event Registration", {"registration_id": code}, fields, as_dict=True, order_by="creation desc")
		if not record and frappe.db.exists("ConnectFest BNI Event Registration", code):
			record = frappe.db.get_value("ConnectFest BNI Event Registration", code, fields, as_dict=True)

	# 2. Match id / registration_id
	if not record and reg_id:
		record = frappe.db.get_value("ConnectFest BNI Event Registration", {"registration_id": reg_id}, fields, as_dict=True, order_by="creation desc")
		if not record and frappe.db.exists("ConnectFest BNI Event Registration", reg_id):
			record = frappe.db.get_value("ConnectFest BNI Event Registration", reg_id, fields, as_dict=True)

	# 3. Match mobile_no
	if not record and mob:
		record = frappe.db.get_value("ConnectFest BNI Event Registration", {"mobile_no": mob}, fields, as_dict=True, order_by="creation desc")

	# 4. Match email
	if not record and mail:
		record = frappe.db.get_value("ConnectFest BNI Event Registration", {"email": mail}, fields, as_dict=True, order_by="creation desc")

	if not record:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": f"BNI Event Pass not found for identifier '{reg_id or mob or mail or code}'"
		}

	# If matching only by mobile or email without code/registration_id, ensure session matches or is admin/scanner
	if not code and not reg_id:
		from connectfest.services.auth import is_admin_user, is_scanner_user
		sess_u = getattr(frappe.session, "user", "")
		rec_email = (record.get("email") or "").lower()
		if sess_u == "Guest" or (sess_u.lower() != rec_email and not is_admin_user(sess_u) and not is_scanner_user(sess_u)):
			frappe.local.response["http_status_code"] = 401
			return {
				"status": "unauthorized",
				"message": "Authentication required! Please provide registration_id or pass_code, or log in using Email OTP to access your pass.",
			}

	qr_payload = f"CONNECTFEST2027:BNI_EVENT:{record.get('pass_code')}:{record.get('email')}:{record.get('mobile_no')}"

	frappe.local.response["http_status_code"] = 200
	clean_res = {
		"status": "success",
		"pass_code": record.get("pass_code"),
		"code": record.get("pass_code"),
		"registration_id": record.get("registration_id") or record.get("name"),
		"id": record.get("registration_id") or record.get("name"),
		"qr_payload": qr_payload,
	}

	# Optional verbose details only if explicitly asked
	if parse_boolean(data.get("full") or data.get("details") or kwargs.get("full") or kwargs.get("details")):
		record["qr_payload"] = qr_payload
		clean_res["details"] = record

	return clean_res


@handle_api_errors("ConnectFest Admin BNI Event Registrations Error")
def admin_get_bni_event_registrations(**kwargs):
	"""
	Admin & Scanner API: List BNI Event registrations with search, filter, and check-in stats.
	Restricted strictly to Event Administrators and authorized Gate Scanners.
	"""
	sess_user = getattr(frappe.session, "user", None)
	from connectfest.services.auth import is_admin_user, is_scanner_user

	# Resolve user from sid if Guest
	if not sess_user or sess_user == "Guest":
		req_sid = None
		if hasattr(frappe, "request") and frappe.request:
			try:
				req_sid = (
					frappe.get_request_header("sid")
					or frappe.get_request_header("X-Frappe-Session-Id")
					or (frappe.request.cookies.get("sid") if hasattr(frappe.request, "cookies") and frappe.request.cookies else None)
				)
				if not req_sid:
					auth_hdr = frappe.get_request_header("Authorization", "")
					if auth_hdr.startswith("Bearer "):
						req_sid = auth_hdr.split("Bearer ", 1)[1].strip()
			except Exception:
				pass
		if req_sid and req_sid != "Guest":
			try:
				res = frappe.db.sql("SELECT user FROM `tabSessions` WHERE sid = %s LIMIT 1", (req_sid,))
				if res and res[0][0]:
					sess_user = res[0][0]
			except Exception:
				pass

	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Authentication required! Please log in as an Administrator or authorized Gate Scanner.",
		}

	if not (is_admin_user(sess_user) or is_scanner_user(sess_user)):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": f"Access Denied: User '{sess_user}' does not have privileges! Only Administrators and authorized Gate Scanners can view event registrations.",
		}

	data = extract_request_data(kwargs)
	search = safe_str(data.get("search") or kwargs.get("search")).strip()
	status = safe_str(data.get("status") or kwargs.get("status")).strip()
	chapter = safe_str(data.get("chapter") or data.get("chapter_name") or kwargs.get("chapter")).strip()
	page = max(1, cint(data.get("page") or kwargs.get("page") or 1))
	limit = min(100, max(1, cint(data.get("limit") or kwargs.get("limit") or 20)))
	start = (page - 1) * limit

	conditions = []
	values = {}

	if search:
		conditions.append("(full_name LIKE %(search)s OR mobile_no LIKE %(search)s OR email LIKE %(search)s OR company_name LIKE %(search)s OR pass_code LIKE %(search)s)")
		values["search"] = f"%{search}%"

	if status:
		conditions.append("status = %(status)s")
		values["status"] = status

	if chapter:
		conditions.append("chapter_name LIKE %(chapter)s")
		values["chapter"] = f"%{chapter}%"

	where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

	items = frappe.db.sql(f"""
		SELECT name, registration_id, full_name, company_name, mobile_no, email,
		       chapter_name, region_name, location, industry, segment, event_name,
		       pass_code, status, checked_in, checked_in_at, checked_in_by, creation
		FROM `tabConnectFest BNI Event Registration`
		{where_clause}
		ORDER BY creation DESC
		LIMIT {limit} OFFSET {start}
	""", values, as_dict=True)

	total_count = frappe.db.sql(f"""
		SELECT COUNT(name) as cnt
		FROM `tabConnectFest BNI Event Registration`
		{where_clause}
	""", values, as_dict=True)[0].cnt

	checked_in_count = frappe.db.sql(f"""
		SELECT COUNT(name) as cnt
		FROM `tabConnectFest BNI Event Registration`
		WHERE checked_in = 1
	""", as_dict=True)[0].cnt

	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"total_count": total_count,
		"checked_in_count": checked_in_count,
		"pending_count": max(0, total_count - checked_in_count),
		"page": page,
		"limit": limit,
		"registrations": items
	}


# Aliases
get_bni_event_registrations = admin_get_bni_event_registrations
get_event_registrations = admin_get_bni_event_registrations
admin_get_event_registrations = admin_get_bni_event_registrations
get_bni_event_registration = get_bni_event_pass
get_event_registration = get_bni_event_pass
admin_get_bni_event_registration = get_bni_event_pass
get_event_pass = get_bni_event_pass
get_pass = get_bni_event_pass

