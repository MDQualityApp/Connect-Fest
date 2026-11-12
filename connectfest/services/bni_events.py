import json
import frappe
from frappe.utils import now_datetime, cint
from connectfest.services.common import extract_request_data, handle_api_errors, safe_str
from connectfest.services.bni_members import normalize_phone_number, is_valid_bni_member
from connectfest.services.registration import validate_registration_email_otp


@handle_api_errors("ConnectFest BNI Event Registration Error")
def register_bni_event(**kwargs):
	"""
	Dedicated BNI Event Registration API.
	Requirements:
	1. Attendee mobile_no MUST exist in 'ConnectFest BNI Member' DocType (Mandatory BNI / CNR member).
	2. Email OTP verification is mandatory (No Mobile OTP required).
	3. Generates unique BNI Event Pass Code & QR Payload for gate check-in.
	4. Completely isolated from existing exhibitor/visitor flows.
	"""
	data = extract_request_data(kwargs)

	full_name = safe_str(data.get("full_name") or data.get("name"))
	company_name = safe_str(data.get("company_name") or data.get("company"))
	mobile_no = normalize_phone_number(data.get("mobile_no") or data.get("phone") or data.get("mobile"))
	email = safe_str(data.get("email")).strip().lower()

	chapter_name = safe_str(data.get("chapter_name") or data.get("chapter"))
	region_name = safe_str(data.get("region_name") or data.get("region"))
	location = safe_str(data.get("location") or data.get("city") or "Chennai")
	industry = safe_str(data.get("industry"))
	segment = safe_str(data.get("segment"))
	event_name = safe_str(data.get("event_name") or "ConnectFest BNI Events 2027")

	errors = []
	if not full_name:
		errors.append("Full Name is required")
	if not email or "@" not in email:
		errors.append("Valid Email Address is required")
	if not mobile_no or len(mobile_no) != 10:
		errors.append("Valid 10-digit Mobile Number is required")

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

	# 1. Mandatory Email OTP Verification (No mobile OTP required)
	email_val_err = validate_registration_email_otp(email, data)
	if email_val_err:
		frappe.local.response["http_status_code"] = 400
		return email_val_err

	# 2. Mandatory BNI Member Verification:
	# Must exist in ConnectFest BNI Member DocType
	is_bni, bni_doc = is_valid_bni_member(mobile_no)
	if not is_bni:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "You are not a BNI member! Registration is strictly restricted to verified BNI members.",
			"mobile_no": mobile_no,
		}

	# Auto-populate chapter / region from BNI Member record if missing
	if bni_doc:
		if not chapter_name and bni_doc.get("chapter_name"):
			chapter_name = bni_doc.get("chapter_name")
		if not region_name and bni_doc.get("region_name"):
			region_name = bni_doc.get("region_name")
		if not full_name and bni_doc.get("member_name"):
			full_name = bni_doc.get("member_name")

	# 3. Check for duplicate registration for this BNI Event
	existing_reg = frappe.db.get_value(
		"ConnectFest BNI Event Registration",
		{"mobile_no": mobile_no},
		["name", "registration_id", "pass_code", "full_name", "email", "status"],
		as_dict=True
	)
	if not existing_reg:
		existing_reg = frappe.db.get_value(
			"ConnectFest BNI Event Registration",
			{"email": email},
			["name", "registration_id", "pass_code", "full_name", "mobile_no", "status"],
			as_dict=True
		)

	if existing_reg:
		frappe.local.response["http_status_code"] = 200
		return {
			"status": "already_registered",
			"message": f"You are already registered for {event_name}!",
			"registration_id": existing_reg.get("registration_id") or existing_reg.get("name"),
			"pass_code": existing_reg.get("pass_code"),
			"attendee": existing_reg,
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

	# 5. Send Email Pass Confirmation
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
			now=True,
			delayed=False,
		)
		frappe.db.commit()
	except Exception as e:
		frappe.logger("connectfest").warning(f"[BNI Event] Failed to send confirmation email to {email}: {str(e)}")

	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"type": "bni_event",
		"message": "BNI Event registration successful! Digital pass has been generated.",
		"registration_id": doc.name,
		"pass_code": pass_code,
		"full_name": full_name,
		"chapter_name": chapter_name,
		"qr_payload": qr_data,
		"data": {
			"name": doc.name,
			"registration_id": doc.name,
			"full_name": full_name,
			"company_name": company_name,
			"mobile_no": mobile_no,
			"email": email,
			"chapter_name": chapter_name,
			"region_name": region_name,
			"location": location,
			"industry": industry,
			"segment": segment,
			"event_name": event_name,
			"pass_code": pass_code,
			"status": "Registered",
			"checked_in": 0,
		}
	}


@handle_api_errors("ConnectFest BNI Event Pass Verify Error")
def verify_bni_event_pass(pass_code=None, **kwargs):
	"""
	Gate Verification / Scanner API for BNI Event Pass (Same as Visitor Pass Check & Verify).
	Accepts pass_code, qr_code, registration_id, or mobile_no.
	Validates pass, prevents duplicate check-in, and records check-in timestamp.
	"""
	data = extract_request_data(kwargs)
	code = safe_str(pass_code or data.get("pass_code") or data.get("qr_code") or data.get("code") or data.get("registration_id"))
	mob = normalize_phone_number(data.get("mobile_no") or data.get("phone") or data.get("mobile"))

	if not code and not mob:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"verified": False,
			"message": "Pass code, Registration ID, or Mobile Number is required for verification."
		}

	# Lookup BNI Event Registration
	record = None
	if code:
		record = frappe.db.get_value(
			"ConnectFest BNI Event Registration",
			{"pass_code": code},
			["name", "registration_id", "full_name", "company_name", "mobile_no", "email", "chapter_name", "region_name", "event_name", "pass_code", "status", "checked_in", "checked_in_at", "checked_in_by"],
			as_dict=True
		)
		if not record:
			record = frappe.db.get_value(
				"ConnectFest BNI Event Registration",
				{"registration_id": code},
				["name", "registration_id", "full_name", "company_name", "mobile_no", "email", "chapter_name", "region_name", "event_name", "pass_code", "status", "checked_in", "checked_in_at", "checked_in_by"],
				as_dict=True
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
			as_dict=True
		)

	if not record:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"verified": False,
			"message": "Invalid BNI Event Pass! No matching registration found.",
			"code_scanned": code or mob
		}

	# Check if already checked in
	now_dt = now_datetime()
	sess_user = getattr(frappe.session, "user", "Gate Staff") or "Gate Staff"

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
	Retrieves BNI Event Pass details and QR payload for an attendee.
	"""
	data = extract_request_data(kwargs)
	code = safe_str(pass_code or data.get("pass_code"))
	reg_id = safe_str(registration_id or data.get("registration_id"))
	mob = normalize_phone_number(mobile_no or data.get("mobile_no") or data.get("phone"))
	mail = safe_str(email or data.get("email")).strip().lower()

	filters = {}
	if code:
		filters["pass_code"] = code
	elif reg_id:
		filters["registration_id"] = reg_id
	elif mob:
		filters["mobile_no"] = mob
	elif mail:
		filters["email"] = mail
	else:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "pass_code, registration_id, mobile_no, or email is required"}

	record = frappe.db.get_value(
		"ConnectFest BNI Event Registration",
		filters,
		["name", "registration_id", "full_name", "company_name", "mobile_no", "email", "chapter_name", "region_name", "location", "industry", "segment", "event_name", "pass_code", "qr_payload", "status", "checked_in", "checked_in_at"],
		as_dict=True
	)
	if not record:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": "BNI Event Pass not found"}

	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"pass": record
	}


@handle_api_errors("ConnectFest Admin BNI Event Registrations Error")
def admin_get_bni_event_registrations(**kwargs):
	"""
	Admin API: List BNI Event registrations with search, filter, and check-in stats.
	"""
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
		       pass_code, status, checked_in, checked_in_at, creation
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

	return {
		"status": "success",
		"total_count": total_count,
		"checked_in_count": checked_in_count,
		"pending_count": max(0, total_count - checked_in_count),
		"page": page,
		"limit": limit,
		"registrations": items
	}
