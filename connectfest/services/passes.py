import os
import io
import re
import urllib.parse
import urllib.request
import frappe
from frappe.utils import now_datetime, flt, cint
from connectfest.services.common import (
	STALL_CACHE_PREFIX,
	VISITOR_CACHE_PREFIX,
	extract_request_data,
	get_cached_record,
	safe_str,
	format_full_url,
	get_base_url,
	log_and_return_error,
)


def admin_verify_gate_pass(pass_code=None, day=None, **kwargs):
	"""
	Universal Gate Entry QR Scanner API.
	- Restricted to Event Administrators and authorized Gate Scanners.
	- Scans and validates BOTH Visitor passes and BNI Event passes (and Exhibitor passes).
	- Validates Day matching for visitor passes.
	- Prevents duplicate check-in with exact timestamp and scanner user details.
	"""
	from connectfest.services.auth import get_user_scanner_scopes, resolve_authenticated_session_user

	sess_user, _ = resolve_authenticated_session_user(**kwargs)

	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"verified": False,
			"message": "Authentication required! Please log in as an Administrator or authorized Gate Scanner.",
		}

	allowed_scopes = get_user_scanner_scopes(sess_user)
	if not allowed_scopes:
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"verified": False,
			"message": "Access Denied: Your account does not have scanner privileges! Contact Event Administrator to grant scanner access.",
		}

	data = extract_request_data(kwargs)
	raw_input_code = str(pass_code or data.get("pass_code") or data.get("qr_code") or data.get("code") or data.get("registration_id") or "").strip()
	code = raw_input_code
	scan_day = (day or data.get("day") or data.get("scan_day") or data.get("which_day") or data.get("current_day") or "").strip()
	mob_scan = (data.get("mobile_no") or data.get("phone") or data.get("mobile") or "").strip()
	email_scan = safe_str(kwargs.get("email") or data.get("email") or data.get("user_email") or data.get("mail")).strip().lower()
	req_pass_index = cint(data.get("pass_index") or data.get("slot") or kwargs.get("pass_index") or 0)

	if not scan_day:
		now_dt = now_datetime()
		date_str = now_dt.strftime("%Y-%m-%d")
		if date_str == "2027-02-21":
			scan_day = "Day 2"
		elif date_str == "2027-02-20":
			scan_day = "Day 1"

	if not code and not mob_scan and not email_scan:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "pass_code, mobile_no, or email is required"}

	is_pass_2_scan = (req_pass_index == 2) or ("-P2" in raw_input_code.upper())

	if code and ":" in code:
		parts = code.split(":")
		found_code = None
		for p in parts:
			p_clean = p.strip()
			if p_clean.startswith("CF27-") or p_clean.startswith("VIS-") or p_clean.startswith("CF-") or p_clean.startswith("BNI-") or p_clean.startswith("EXH-"):
				found_code = p_clean
				break
		if found_code:
			code = found_code
		elif len(parts) >= 4 and parts[1].upper() == "EXHIBITOR":
			code = parts[3].strip() or parts[2].strip()
		elif len(parts) >= 2:
			code = parts[1].strip()

	if code.startswith("{") and code.endswith("}"):
		try:
			import json
			_obj = json.loads(code)
			code = str(_obj.get("pass_code") or _obj.get("code") or _obj.get("registration_id") or code).strip()
			if not mob_scan:
				mob_scan = str(_obj.get("mobile") or _obj.get("mobile_no") or _obj.get("phone") or "").strip()
			if not email_scan:
				email_scan = safe_str(_obj.get("email") or _obj.get("mail")).strip().lower()
		except Exception:
			pass

	clean_digits = re.sub(r"[^\d]", "", code)
	if not mob_scan and len(clean_digits) in (10, 11, 12, 13):
		mob_scan = clean_digits[-10:]

	if not email_scan and "@" in code and "." in code:
		email_scan = code.lower().strip()

	if ("-P2" in code.upper()) or (code.endswith("-2") and not code.startswith("CF27-")):
		is_pass_2_scan = True

	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
	denied_match = None

	def _deny_scan_scope(pass_type_key, label):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"verified": False,
			"pass_type": pass_type_key,
			"message": f"Access Denied: Your account is not authorized to scan {label} passes.",
		}

	# =========================================================================
	# 1. Check Exhibitor Stall Pass (ConnectFest Registration - Max 2 Passes)
	# =========================================================================
	exhibitor = None
	clean_base_code = code.rsplit("-", 1)[0] if ("-P2" in code.upper() or code.endswith("-2")) else code
	no_hyphen_code = code.replace("-", "").strip().upper()

	has_exh_pass_col = frappe.db.has_column("ConnectFest Registration", "exhibitor_pass_url") if hasattr(frappe, "db") and frappe.db else True
	exh_pass_col_sql = ", exhibitor_pass_url, pass_2_pass_url" if has_exh_pass_col else ""

	if is_pass_2_scan:
		exhibitor = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, company_name, company_logo, location, mobile_no, email,
			       stall_number, stall_category, payment_status, status, pass_code, qr_payload,
			       checked_in, checked_in_at, checked_in_by, pass_1_image,
			       pass_2_name, pass_2_image, pass_2_mobile, pass_2_email, pass_2_pass_code, pass_2_qr_payload,
			       pass_2_checked_in, pass_2_checked_in_at, pass_2_checked_in_by
			       {exh_pass_col_sql}
			FROM `tabConnectFest Registration`
			WHERE pass_2_pass_code = %(code)s
			   OR REPLACE(pass_2_pass_code, '-', '') = %(no_hyphen)s
			   OR (pass_2_pass_code LIKE %(p2_like)s)
			   OR registration_id = %(base)s
			   OR name = %(base)s
			   OR stall_number = %(base)s
			LIMIT 1
		""", {"code": code, "no_hyphen": no_hyphen_code, "p2_like": f"%{code}%", "base": clean_base_code}, as_dict=True)

	if not exhibitor and code:
		exhibitor = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, company_name, company_logo, location, mobile_no, email,
			       stall_number, stall_category, payment_status, status, pass_code, qr_payload,
			       checked_in, checked_in_at, checked_in_by, pass_1_image,
			       pass_2_name, pass_2_image, pass_2_mobile, pass_2_email, pass_2_pass_code, pass_2_qr_payload,
			       pass_2_checked_in, pass_2_checked_in_at, pass_2_checked_in_by
			       {exh_pass_col_sql}
			FROM `tabConnectFest Registration`
			WHERE pass_code = %(code)s 
			   OR REPLACE(pass_code, '-', '') = %(no_hyphen)s
			   OR pass_2_pass_code = %(code)s
			   OR REPLACE(pass_2_pass_code, '-', '') = %(no_hyphen)s
			   OR registration_id = %(code)s 
			   OR name = %(code)s 
			   OR stall_number = %(code)s
			LIMIT 1
		""", {"code": code, "no_hyphen": no_hyphen_code}, as_dict=True)

	if not exhibitor and mob_scan:
		exhibitor = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, company_name, company_logo, location, mobile_no, email,
			       stall_number, stall_category, payment_status, status, pass_code, qr_payload,
			       checked_in, checked_in_at, checked_in_by, pass_1_image,
			       pass_2_name, pass_2_image, pass_2_mobile, pass_2_email, pass_2_pass_code, pass_2_qr_payload,
			       pass_2_checked_in, pass_2_checked_in_at, pass_2_checked_in_by
			       {exh_pass_col_sql}
			FROM `tabConnectFest Registration`
			WHERE mobile_no = %(mob)s OR pass_2_mobile = %(mob)s
			ORDER BY creation DESC LIMIT 1
		""", {"mob": mob_scan}, as_dict=True)

	if not exhibitor and email_scan:
		exhibitor = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, company_name, company_logo, location, mobile_no, email,
			       stall_number, stall_category, payment_status, status, pass_code, qr_payload,
			       checked_in, checked_in_at, checked_in_by, pass_1_image,
			       pass_2_name, pass_2_image, pass_2_mobile, pass_2_email, pass_2_pass_code, pass_2_qr_payload,
			       pass_2_checked_in, pass_2_checked_in_at, pass_2_checked_in_by
			       {exh_pass_col_sql}
			FROM `tabConnectFest Registration`
			WHERE email = %(email)s OR pass_2_email = %(email)s
			ORDER BY creation DESC LIMIT 1
		""", {"email": email_scan}, as_dict=True)

	if exhibitor and "exhibitor" not in allowed_scopes:
		if code:
			return _deny_scan_scope("exhibitor_pass", "Exhibitor")
		denied_match = denied_match or ("exhibitor_pass", "Exhibitor")
		exhibitor = None

	if exhibitor:
		rec = exhibitor[0]

		# Check Stall Allocation
		if not rec.stall_number:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "no_stall",
				"verified": False,
				"pass_type": "exhibitor_pass",
				"message": "❌ ACCESS DENIED: Exhibitor has not selected or been assigned a stall number.",
			}

		# Check Payment Status
		if rec.payment_status != "Paid":
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "payment_pending",
				"verified": False,
				"pass_type": "exhibitor_pass",
				"message": f"❌ ACCESS DENIED (Payment Pending): Stall '{rec.stall_number}' payment is pending ({rec.payment_status or 'Pending'}). Gate entry requires confirmed payment.",
			}

		zone_map = {
			"Silver": "Zone D - East Pavilions",
			"Premium": "Zone C - Center Aisles",
			"Standard": "Zone B - West Aisles",
			"Regular": "Zone E - South Entrance Corridor",
		}
		zone = zone_map.get(rec.stall_category) or "Zone B - West Aisles"

		# Determine whether this scan evaluates Pass 2 (Co-Exhibitor) or Pass 1 (Primary)
		p2_code_val = (rec.get("pass_2_pass_code") or "").strip().upper()
		check_as_pass_2 = is_pass_2_scan or (code and (code.upper() == p2_code_val or (p2_code_val and no_hyphen_code == p2_code_val.replace("-", ""))))
		if not check_as_pass_2 and (mob_scan and rec.get("pass_2_mobile") and mob_scan == rec.get("pass_2_mobile") and mob_scan != rec.get("mobile_no")):
			check_as_pass_2 = True
		if not check_as_pass_2 and (email_scan and rec.get("pass_2_email") and email_scan == rec.get("pass_2_email") and email_scan != rec.get("email")):
			check_as_pass_2 = True
		is_specific_pass_1_code = bool(code and (code == rec.get("pass_code") or code == rec.get("registration_id") or code == rec.name))
		if not check_as_pass_2 and not is_specific_pass_1_code and cint(rec.get("checked_in")) and not cint(rec.get("pass_2_checked_in")) and rec.get("pass_2_name"):
			check_as_pass_2 = True

		if check_as_pass_2:
			# === Pass 2 (Co-Exhibitor / Booth Staff) Verification ===
			if not (rec.get("pass_2_name") or "").strip():
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "unassigned",
					"verified": False,
					"pass_type": "exhibitor_pass",
					"pass_index": 2,
					"message": f"❌ ACCESS DENIED: Co-Exhibitor Pass 2 for Stall '{rec.stall_number}' is not yet assigned to a person. Primary exhibitor ({rec.full_name}) must set friend/staff name and photo first.",
				}

			# Auto-generate or resolve Pass 2 Badge Image
			pass_2_badge = rec.get("pass_2_pass_url") or ""
			if not pass_2_badge and rec.get("pass_2_name"):
				try:
					from connectfest.services.exhibitor_badge import generate_exhibitor_badge_image
					pass_2_badge = generate_exhibitor_badge_image(rec, pass_index=2)
					if pass_2_badge and frappe.db.has_column("ConnectFest Registration", "pass_2_pass_url"):
						frappe.db.set_value("ConnectFest Registration", rec.get("name"), "pass_2_pass_url", pass_2_badge)
						frappe.db.commit()
				except Exception:
					pass
			badge_2_url = format_full_url(pass_2_badge) if pass_2_badge else ""

			if cint(rec.get("pass_2_checked_in")):
				return {
					"status": "duplicate_entry",
					"verified": False,
					"pass_type": "exhibitor_pass",
					"pass_index": 2,
					"exhibitor_pass_url": badge_2_url,
					"message": f"⚠️ ALREADY SCANNED! Co-Exhibitor Pass 2 ({rec.pass_2_name}) for Stall {rec.stall_number} was already scanned at {rec.pass_2_checked_in_at} by {rec.get('pass_2_checked_in_by') or 'Gate Staff'}. Duplicate entry denied.",
					"pass": {
						"registration_id": rec.registration_id or rec.name,
						"pass_code": rec.get("pass_2_pass_code") or f"{rec.registration_id}-P2",
						"pass_index": 2,
						"pass_title": "Co-Exhibitor Pass (Booth Staff)",
						"full_name": rec.pass_2_name,
						"image": format_full_url(rec.pass_2_image),
						"visitor_image": format_full_url(rec.pass_2_image),
						"exhibitor_pass_url": badge_2_url,
						"stall_number": rec.stall_number,
						"stall_category": rec.stall_category,
						"payment_status": rec.payment_status,
						"checked_in_at": str(rec.pass_2_checked_in_at or ""),
						"checked_in_by": rec.get("pass_2_checked_in_by") or "Gate Staff",
						"venue": "Chennai Trade Centre, Nandambakkam",
					},
				}

			frappe.db.set_value("ConnectFest Registration", rec.name, {
				"pass_2_checked_in": 1,
				"pass_2_checked_in_at": now_str,
				"pass_2_checked_in_by": sess_user,
			})
			frappe.db.commit()

			return {
				"status": "success",
				"verified": True,
				"pass_type": "exhibitor_pass",
				"pass_index": 2,
				"pass_title": "Co-Exhibitor Pass (Booth Staff)",
				"exhibitor_pass_url": badge_2_url,
				"message": f"✅ CO-EXHIBITOR PASS VERIFIED - WELCOME {rec.pass_2_name.upper()}! STALL {rec.stall_number} ({rec.stall_category})",
				"pass": {
					"pass_code": rec.get("pass_2_pass_code") or f"{rec.registration_id}-P2",
					"pass_index": 2,
					"pass_title": "Co-Exhibitor Pass (Booth Staff)",
					"registration_id": rec.registration_id or rec.name,
					"full_name": rec.pass_2_name,
					"image": format_full_url(rec.pass_2_image),
					"visitor_image": format_full_url(rec.pass_2_image),
					"exhibitor_pass_url": badge_2_url,
					"company_name": rec.company_name,
					"company_logo": format_full_url(rec.company_logo),
					"location": rec.location or "",
					"stall_number": rec.stall_number,
					"stall_category": rec.stall_category,
					"zone": zone,
					"access": "EXHIBITOR - ALL AREAS & SETUP",
					"which_day": "Both days",
					"payment_status": rec.payment_status,
					"mobile_no": rec.get("pass_2_mobile") or rec.mobile_no,
					"email": rec.get("pass_2_email") or rec.email,
					"checked_in": 1,
					"checked_in_at": now_str,
					"checked_in_by": sess_user,
					"venue": "Chennai Trade Centre, Nandambakkam",
					"dates": "20 & 21 Feb 2027",
				},
			}

		else:
			# === Pass 1 (Primary Exhibitor) Verification ===
			# Auto-generate or resolve Pass 1 Badge Image
			pass_1_badge = rec.get("exhibitor_pass_url") or ""
			if not pass_1_badge:
				try:
					from connectfest.services.exhibitor_badge import generate_exhibitor_badge_image
					pass_1_badge = generate_exhibitor_badge_image(rec, pass_index=1)
					if pass_1_badge and frappe.db.has_column("ConnectFest Registration", "exhibitor_pass_url"):
						frappe.db.set_value("ConnectFest Registration", rec.get("name"), "exhibitor_pass_url", pass_1_badge)
						frappe.db.commit()
				except Exception:
					pass
			badge_1_url = format_full_url(pass_1_badge) if pass_1_badge else ""

			if cint(rec.checked_in):
				p2_info = f" (Note: Co-Exhibitor Pass 2 for {rec.pass_2_name} is still unused)" if (rec.get("pass_2_name") and not cint(rec.get("pass_2_checked_in"))) else ""
				return {
					"status": "duplicate_entry",
					"verified": False,
					"pass_type": "exhibitor_pass",
					"pass_index": 1,
					"exhibitor_pass_url": badge_1_url,
					"message": f"⚠️ ALREADY SCANNED! Primary Exhibitor pass ({rec.pass_code or rec.registration_id} - {rec.full_name}) was already scanned at {rec.checked_in_at} by {rec.get('checked_in_by') or 'Gate Staff'}. Duplicate entry denied.{p2_info}",
					"pass": {
						"registration_id": rec.registration_id or rec.name,
						"pass_code": rec.pass_code or rec.registration_id,
						"pass_index": 1,
						"pass_title": "Primary Exhibitor Pass",
						"full_name": rec.full_name,
						"company_name": rec.company_name,
						"company_logo": format_full_url(rec.company_logo),
						"exhibitor_pass_url": badge_1_url,
						"stall_number": rec.stall_number,
						"stall_category": rec.stall_category,
						"payment_status": rec.payment_status,
						"checked_in_at": str(rec.checked_in_at or ""),
						"checked_in_by": rec.get("checked_in_by") or "Gate Staff",
						"venue": "Chennai Trade Centre, Nandambakkam",
					},
				}

			frappe.db.set_value("ConnectFest Registration", rec.name, {
				"checked_in": 1,
				"checked_in_at": now_str,
				"checked_in_by": sess_user,
				"status": "Attended",
			})
			frappe.db.commit()

			return {
				"status": "success",
				"verified": True,
				"pass_type": "exhibitor_pass",
				"pass_index": 1,
				"pass_title": "Primary Exhibitor Pass",
				"exhibitor_pass_url": badge_1_url,
				"message": f"✅ EXHIBITOR PASS VERIFIED - WELCOME {rec.full_name.upper()}! STALL {rec.stall_number} ({rec.stall_category})",
				"pass": {
					"pass_code": rec.pass_code or rec.registration_id or rec.name,
					"pass_index": 1,
					"pass_title": "Primary Exhibitor Pass",
					"registration_id": rec.registration_id or rec.name,
					"full_name": rec.full_name,
					"company_name": rec.company_name,
					"company_logo": format_full_url(rec.company_logo),
					"visitor_image": format_full_url(rec.get("pass_1_image") or rec.company_logo),
					"image": format_full_url(rec.get("pass_1_image") or rec.company_logo),
					"exhibitor_pass_url": badge_1_url,
					"location": rec.location or "",
					"stall_number": rec.stall_number,
					"stall_category": rec.stall_category,
					"zone": zone,
					"access": "EXHIBITOR - ALL AREAS & SETUP",
					"which_day": "Both days",
					"payment_status": rec.payment_status,
					"mobile_no": rec.mobile_no,
					"email": rec.email,
					"checked_in": 1,
					"checked_in_at": now_str,
					"checked_in_by": sess_user,
					"venue": "Chennai Trade Centre, Nandambakkam",
					"dates": "20 & 21 Feb 2027",
				},
			}

	# =========================================================================
	# 2. Check BNI Event Pass (ConnectFest BNI Event Registration)
	# =========================================================================
	bni_event = None
	has_ev_col = frappe.db.has_column("ConnectFest BNI Event Registration", "event_pass_url") if hasattr(frappe, "db") and frappe.db else True
	ev_col_sql = ", event_pass_url" if has_ev_col else ""

	if code:
		bni_event = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, company_name, mobile_no, email,
			       chapter_name, region_name, location, industry, segment, event_name,
			       pass_code, status, checked_in, checked_in_at, checked_in_by
			       {ev_col_sql}
			FROM `tabConnectFest BNI Event Registration`
			WHERE pass_code = %(code)s OR registration_id = %(code)s OR name = %(code)s
			LIMIT 1
		""", {"code": code}, as_dict=True)

	if not bni_event and mob_scan:
		bni_event = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, company_name, mobile_no, email,
			       chapter_name, region_name, location, industry, segment, event_name,
			       pass_code, status, checked_in, checked_in_at, checked_in_by
			       {ev_col_sql}
			FROM `tabConnectFest BNI Event Registration`
			WHERE mobile_no = %(mob)s
			ORDER BY creation DESC LIMIT 1
		""", {"mob": mob_scan}, as_dict=True)

	if not bni_event and email_scan:
		bni_event = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, company_name, mobile_no, email,
			       chapter_name, region_name, location, industry, segment, event_name,
			       pass_code, status, checked_in, checked_in_at, checked_in_by
			       {ev_col_sql}
			FROM `tabConnectFest BNI Event Registration`
			WHERE email = %(email)s
			ORDER BY creation DESC LIMIT 1
		""", {"email": email_scan}, as_dict=True)

	if bni_event and "event" not in allowed_scopes:
		if code:
			return _deny_scan_scope("bni_event_pass", "BNI Event")
		denied_match = denied_match or ("bni_event_pass", "BNI Event")
		bni_event = None

	if bni_event and "event" not in allowed_scopes:
		if code:
			return _deny_scan_scope("bni_event_pass", "BNI Event")
		denied_match = denied_match or ("bni_event_pass", "BNI Event")
		bni_event = None

	if bni_event:
		rec = bni_event[0]

		raw_ev_pass = rec.get("event_pass_url") or ""
		if not raw_ev_pass:
			try:
				from connectfest.services.event_badge import generate_event_badge_image
				raw_ev_pass = generate_event_badge_image(rec)
				if raw_ev_pass and frappe.db.has_column("ConnectFest BNI Event Registration", "event_pass_url"):
					frappe.db.set_value("ConnectFest BNI Event Registration", rec.get("name"), "event_pass_url", raw_ev_pass)
					frappe.db.commit()
			except Exception:
				pass
		ev_pass_url = format_full_url(raw_ev_pass) if raw_ev_pass else ""

		# Duplicate Scan Check
		if rec.checked_in:
			return {
				"status": "duplicate_entry",
				"verified": False,
				"pass_type": "bni_event_pass",
				"pass_title": "BNI Event Pass",
				"event_pass_url": ev_pass_url,
				"message": f"⚠️ ALREADY SCANNED! BNI Event pass ({rec.pass_code}) was already scanned at {rec.checked_in_at} by {rec.get('checked_in_by') or 'Gate Staff'}. Duplicate entry denied.",
				"pass": {
					"pass_code": rec.pass_code,
					"registration_id": rec.registration_id or rec.name,
					"full_name": rec.full_name,
					"company_name": rec.company_name,
					"chapter_name": rec.chapter_name,
					"region_name": rec.region_name,
					"mobile_no": rec.mobile_no,
					"email": rec.email,
					"event_name": rec.event_name,
					"event_pass_url": ev_pass_url,
					"checked_in": 1,
					"checked_in_at": str(rec.checked_in_at or ""),
					"checked_in_by": rec.get("checked_in_by") or "Gate Staff",
					"venue": "Chennai Trade Centre, Nandambakkam",
				},
			}

		frappe.db.set_value("ConnectFest BNI Event Registration", rec.name, {
			"checked_in": 1,
			"checked_in_at": now_str,
			"checked_in_by": sess_user,
			"status": "Checked In",
		})
		frappe.db.commit()

		return {
			"status": "success",
			"verified": True,
			"pass_type": "bni_event_pass",
			"event_pass_url": ev_pass_url,
			"message": f"✅ BNI EVENT PASS VERIFIED - WELCOME TO {rec.get('event_name') or 'CONNECTFEST BNI EVENTS'}!",
			"pass": {
				"pass_code": rec.pass_code,
				"registration_id": rec.registration_id or rec.name,
				"full_name": rec.full_name,
				"company_name": rec.company_name,
				"chapter_name": rec.chapter_name,
				"region_name": rec.region_name,
				"mobile_no": rec.mobile_no,
				"email": rec.email,
				"event_name": rec.event_name,
				"event_pass_url": ev_pass_url,
				"checked_in": 1,
				"checked_in_at": now_str,
				"checked_in_by": sess_user,
				"venue": "Chennai Trade Centre, Nandambakkam",
			},
		}

	# =========================================================================
	# 3. Check Visitor Pass (ConnectFest Visitor Registration)
	# =========================================================================
	visitor = None
	has_v_col = frappe.db.has_column("ConnectFest Visitor Registration", "visitor_image_pass") if hasattr(frappe, "db") and frappe.db else True
	v_col_sql = ", visitor_image_pass" if has_v_col else ""

	if code:
		visitor = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code, status, checked_in, checked_in_at, checked_in_by, area_of_interest
			       {v_col_sql}
			FROM `tabConnectFest Visitor Registration`
			WHERE pass_code = %(code)s OR registration_id = %(code)s OR name = %(code)s
			LIMIT 1
		""", {"code": code}, as_dict=True)

	if not visitor and mob_scan:
		visitor = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code, status, checked_in, checked_in_at, checked_in_by, area_of_interest
			       {v_col_sql}
			FROM `tabConnectFest Visitor Registration`
			WHERE mobile_no = %(mob)s
			ORDER BY creation DESC LIMIT 1
		""", {"mob": mob_scan}, as_dict=True)

	if not visitor and email_scan:
		visitor = frappe.db.sql(f"""
			SELECT name, registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code, status, checked_in, checked_in_at, checked_in_by, area_of_interest
			       {v_col_sql}
			FROM `tabConnectFest Visitor Registration`
			WHERE email = %(email)s
			ORDER BY creation DESC LIMIT 1
		""", {"email": email_scan}, as_dict=True)

	if visitor and "visitor" not in allowed_scopes:
		if code:
			return _deny_scan_scope("visitor_pass", "Visitor")
		denied_match = denied_match or ("visitor_pass", "Visitor")
		visitor = None

	if visitor:
		rec = visitor[0]

		raw_v_pass = rec.get("visitor_image_pass") or ""
		if not raw_v_pass:
			try:
				from connectfest.services.visitor_badge import generate_visitor_badge_image
				raw_v_pass = generate_visitor_badge_image(rec)
				if raw_v_pass and frappe.db.has_column("ConnectFest Visitor Registration", "visitor_image_pass"):
					frappe.db.set_value("ConnectFest Visitor Registration", rec.get("name"), "visitor_image_pass", raw_v_pass)
					frappe.db.commit()
			except Exception:
				pass
		vis_pass_url = format_full_url(raw_v_pass or rec.visitor_image)

		# Day Validation: Day 1 vs Day 2 check
		if scan_day:
			norm_scan = "Day 1" if "1" in str(scan_day) else ("Day 2" if "2" in str(scan_day) else str(scan_day).strip())
			vis_day = (rec.which_day or "Both days").strip()
			norm_vis = "Day 1" if "1" in vis_day else ("Day 2" if "2" in vis_day else vis_day)

			if norm_vis.lower() != "both days" and norm_scan.lower() != norm_vis.lower():
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "invalid_day",
					"verified": False,
					"message": f"❌ ACCESS DENIED (Day Mismatch): This pass is valid for {vis_day} only! It cannot be scanned on {norm_scan}.",
				}

		# Duplicate Scan Check
		if rec.checked_in:
			return {
				"status": "duplicate_entry",
				"verified": False,
				"pass_type": "visitor_pass",
				"pass_title": "Visitor Gate Pass",
				"visitor_pass_url": vis_pass_url,
				"message": f"⚠️ ALREADY SCANNED! Visitor pass ({rec.pass_code} - {rec.full_name}) was already scanned at {rec.checked_in_at} by {rec.get('checked_in_by') or 'Gate Staff'}. Duplicate entry denied.",
				"pass": {
					"pass_code": rec.pass_code,
					"registration_id": rec.registration_id or rec.name,
					"pass_type": "visitor_pass",
					"pass_title": "Visitor Gate Pass",
					"full_name": rec.full_name,
					"visitor_pass_url": vis_pass_url,
					"visitor_image": format_full_url(rec.visitor_image),
					"which_day": rec.which_day or "Both days",
					"access": "VISITOR - BOTH DAYS" if (not rec.which_day or rec.which_day.lower() == "both days") else f"VISITOR - {rec.which_day.upper()}",
					"email": rec.email,
					"mobile_no": rec.mobile_no,
					"area_of_interest": rec.get("area_of_interest") or "",
					"checked_in": 1,
					"checked_in_at": str(rec.checked_in_at or ""),
					"checked_in_by": rec.get("checked_in_by") or "Gate Staff",
					"venue": "Chennai Trade Centre, Nandambakkam",
					"dates": "20 & 21 Feb 2027",
					"qr_payload": rec.get("qr_payload") or rec.pass_code,
					"status": "Checked In",
				},
			}

		frappe.db.set_value("ConnectFest Visitor Registration", rec.name, {
			"checked_in": 1,
			"checked_in_at": now_str,
			"checked_in_by": sess_user,
			"status": "Checked In",
		})
		frappe.db.commit()

		return {
			"status": "success",
			"verified": True,
			"pass_type": "visitor_pass",
			"pass_title": "Visitor Gate Pass",
			"visitor_pass_url": vis_pass_url,
			"message": f"✅ VISITOR PASS VERIFIED - WELCOME {rec.full_name.upper()} TO CONNECTFEST 2027!",
			"pass": {
				"pass_code": rec.pass_code,
				"registration_id": rec.registration_id or rec.name,
				"pass_type": "visitor_pass",
				"pass_title": "Visitor Gate Pass",
				"full_name": rec.full_name,
				"visitor_pass_url": vis_pass_url,
				"visitor_image": format_full_url(rec.visitor_image),
				"which_day": rec.which_day or "Both days",
				"access": "VISITOR - BOTH DAYS" if (not rec.which_day or rec.which_day.lower() == "both days") else f"VISITOR - {rec.which_day.upper()}",
				"email": rec.email,
				"mobile_no": rec.mobile_no,
				"area_of_interest": rec.get("area_of_interest") or "",
				"checked_in": 1,
				"checked_in_at": now_str,
				"checked_in_by": sess_user,
				"venue": "Chennai Trade Centre, Nandambakkam",
				"dates": "20 & 21 Feb 2027",
				"qr_payload": rec.get("qr_payload") or rec.pass_code,
				"status": "Checked In",
			},
		}

	if denied_match:
		return _deny_scan_scope(denied_match[0], denied_match[1])

	# Pass Not Found
	frappe.local.response["http_status_code"] = 404
	return {
		"status": "invalid",
		"verified": False,
		"message": f"❌ INVALID PASS: No registration found for '{code or mob_scan}'. Please proceed to help desk.",
	}


def verify_gate_pass(pass_code=None, day=None, **kwargs):
	"""Alias pointing to admin_verify_gate_pass."""
	return admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


def admin_verify_pass(pass_code=None, day=None, **kwargs):
	"""Alias pointing to admin_verify_gate_pass."""
	return admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


def verify_pass(pass_code=None, day=None, **kwargs):
	"""Alias pointing to admin_verify_gate_pass."""
	return admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


def get_scanned_users(status=None, pass_type=None, search=None, scanned_by=None, page=1, limit=50, **kwargs):
	"""
	Scanned & Registered Attendees Verification Dashboard.
	Access strictly restricted to:
	1. Users with dedicated role 'ConnectFest Gate Scanner'
	2. Event Administrators ('Administrator', 'System Manager', 'ConnectFest Admin')

	Allows viewing:
	- All registered attendees (status='all')
	- Only scanned attendees / checked-in (status='scanned' or status='checked_in')
	- Attendees who have NOT been scanned yet / pending (status='not_scanned' or status='pending')
	- Filter by pass_type: 'visitor', 'bni_event', 'all'
	- Filter by who scanned: scanned_by='karthi@gmail.com'
	- Search by name, email, phone, pass code, company
	"""
	from connectfest.services.auth import get_user_scanner_scopes, resolve_authenticated_session_user

	sess_user, _ = resolve_authenticated_session_user(**kwargs)

	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Authentication required! Please log in as an Administrator or authorized Gate Scanner.",
		}

	allowed_scopes = get_user_scanner_scopes(sess_user)
	if not allowed_scopes:
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": "Access Denied: Your account does not have scanner privileges!",
		}

	from frappe.utils import cint
	data = extract_request_data(kwargs)
	filter_status = (status or data.get("status") or "").strip().lower()
	filter_type = (pass_type or data.get("pass_type") or data.get("type") or "all").strip().lower()
	search_q = (search or data.get("search") or data.get("q") or "").strip()
	scanner_filter = (scanned_by or data.get("scanned_by") or data.get("checked_in_by") or "").strip().lower()

	requested_scope = None
	if filter_type in ("visitor", "visitors"):
		requested_scope = "visitor"
	elif filter_type in ("bni_event", "bni", "event", "events"):
		requested_scope = "event"
	elif filter_type in ("exhibitor", "exhibitors", "stall", "stalls"):
		requested_scope = "exhibitor"

	if requested_scope and requested_scope not in allowed_scopes:
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": f"Access Denied: Your account is not authorized to view {requested_scope} pass scan logs.",
		}

	p = max(1, cint(data.get("page") or page or 1))
	l = min(500, max(1, cint(data.get("limit") or limit or 50)))

	records = []

	# 1. Fetch from Visitor Registrations
	if filter_type in ("all", "visitor", "visitors") and "visitor" in allowed_scopes:
		vis_conds = []
		vis_vals = {}
		if filter_status in ("scanned", "checked_in", "checkedin", "1"):
			vis_conds.append("checked_in = 1")
		elif filter_status in ("not_scanned", "pending", "unscanned", "notscanned", "0"):
			vis_conds.append("(checked_in = 0 OR checked_in IS NULL)")

		if scanner_filter:
			vis_conds.append("checked_in_by = %(scanner)s")
			vis_vals["scanner"] = scanner_filter

		if search_q:
			vis_conds.append("(full_name LIKE %(q)s OR mobile_no LIKE %(q)s OR email LIKE %(q)s OR pass_code LIKE %(q)s)")
			vis_vals["q"] = f"%{search_q}%"

		vis_where = f"WHERE {' AND '.join(vis_conds)}" if vis_conds else ""
		vis_sql = f"""
			SELECT name, registration_id, full_name, email, mobile_no, which_day, pass_code,
			       area_of_interest, status, checked_in, checked_in_at, checked_in_by, creation
			FROM `tabConnectFest Visitor Registration`
			{vis_where}
			ORDER BY creation DESC
		"""
		vis_rows = frappe.db.sql(vis_sql, vis_vals, as_dict=True)
		for r in vis_rows:
			records.append({
				"id": r.get("registration_id") or r.get("name"),
				"name": r.get("registration_id") or r.get("name"),
				"full_name": r.get("full_name"),
				"email": r.get("email"),
				"mobile_no": r.get("mobile_no"),
				"pass_type": "visitor_pass",
				"pass_code": r.get("pass_code"),
				"details": f"Day: {r.get('which_day') or 'Both days'}",
				"which_day": r.get("which_day"),
				"area_of_interest": r.get("area_of_interest"),
				"checked_in": cint(r.get("checked_in")),
				"checked_in_at": str(r.get("checked_in_at")) if r.get("checked_in_at") else None,
				"checked_in_by": r.get("checked_in_by"),
				"status": "Checked In" if cint(r.get("checked_in")) else "Not Scanned",
				"creation": str(r.get("creation")),
			})

	# 2. Fetch from BNI Event Registrations
	if filter_type in ("all", "bni_event", "bni", "event", "events") and "event" in allowed_scopes:
		bni_conds = []
		bni_vals = {}
		if filter_status in ("scanned", "checked_in", "checkedin", "1"):
			bni_conds.append("checked_in = 1")
		elif filter_status in ("not_scanned", "pending", "unscanned", "notscanned", "0"):
			bni_conds.append("(checked_in = 0 OR checked_in IS NULL)")

		if scanner_filter:
			bni_conds.append("checked_in_by = %(scanner)s")
			bni_vals["scanner"] = scanner_filter

		if search_q:
			bni_conds.append("(full_name LIKE %(q)s OR mobile_no LIKE %(q)s OR email LIKE %(q)s OR pass_code LIKE %(q)s OR company_name LIKE %(q)s OR chapter_name LIKE %(q)s)")
			bni_vals["q"] = f"%{search_q}%"

		bni_where = f"WHERE {' AND '.join(bni_conds)}" if bni_conds else ""
		bni_sql = f"""
			SELECT name, registration_id, full_name, company_name, mobile_no, email,
			       chapter_name, region_name, location, industry, segment, event_name,
			       pass_code, status, checked_in, checked_in_at, checked_in_by, creation
			FROM `tabConnectFest BNI Event Registration`
			{bni_where}
			ORDER BY creation DESC
		"""
		bni_rows = frappe.db.sql(bni_sql, bni_vals, as_dict=True)
		for r in bni_rows:
			records.append({
				"id": r.get("registration_id") or r.get("name"),
				"name": r.get("registration_id") or r.get("name"),
				"full_name": r.get("full_name"),
				"company_name": r.get("company_name"),
				"email": r.get("email"),
				"mobile_no": r.get("mobile_no"),
				"pass_type": "bni_event_pass",
				"pass_code": r.get("pass_code"),
				"chapter_name": r.get("chapter_name"),
				"region_name": r.get("region_name"),
				"event_name": r.get("event_name"),
				"details": f"Chapter: {r.get('chapter_name') or 'BNI'} ({r.get('company_name') or 'N/A'})",
				"checked_in": cint(r.get("checked_in")),
				"checked_in_at": str(r.get("checked_in_at")) if r.get("checked_in_at") else None,
				"checked_in_by": r.get("checked_in_by"),
				"status": "Checked In" if cint(r.get("checked_in")) else "Not Scanned",
				"creation": str(r.get("creation")),
			})

	# 3. Fetch from Exhibitor Registrations
	if filter_type in ("all", "exhibitor", "exhibitors", "stall", "stalls") and "exhibitor" in allowed_scopes:
		ex_conds = ["payment_status = 'Paid'"]
		ex_vals = {}
		if filter_status in ("scanned", "checked_in", "checkedin", "1"):
			ex_conds.append("checked_in = 1")
		elif filter_status in ("not_scanned", "pending", "unscanned", "notscanned", "0"):
			ex_conds.append("(checked_in = 0 OR checked_in IS NULL)")

		if scanner_filter:
			ex_conds.append("checked_in_by = %(scanner)s")
			ex_vals["scanner"] = scanner_filter

		if search_q:
			ex_conds.append("(full_name LIKE %(q)s OR mobile_no LIKE %(q)s OR email LIKE %(q)s OR pass_code LIKE %(q)s OR company_name LIKE %(q)s OR stall_number LIKE %(q)s)")
			ex_vals["q"] = f"%{search_q}%"

		ex_where = f"WHERE {' AND '.join(ex_conds)}"
		ex_sql = f"""
			SELECT name, registration_id, full_name, company_name, company_logo, mobile_no, email,
			       stall_number, stall_category, pass_code, status, checked_in, checked_in_at, checked_in_by,
			       pass_1_image, pass_2_name, pass_2_image, pass_2_mobile, pass_2_email,
			       pass_2_pass_code, pass_2_checked_in, pass_2_checked_in_at, pass_2_checked_in_by, creation
			FROM `tabConnectFest Registration`
			{ex_where}
			ORDER BY creation DESC
		"""
		ex_rows = frappe.db.sql(ex_sql, ex_vals, as_dict=True)
		for r in ex_rows:
			# Pass 1: Primary Exhibitor Pass
			records.append({
				"id": r.get("registration_id") or r.get("name"),
				"name": r.get("registration_id") or r.get("name"),
				"pass_index": 1,
				"pass_title": "Primary Exhibitor Pass",
				"full_name": r.get("full_name"),
				"company_name": r.get("company_name"),
				"company_logo": format_full_url(r.get("company_logo")),
				"visitor_image": format_full_url(r.get("pass_1_image") or r.get("company_logo")),
				"email": r.get("email"),
				"mobile_no": r.get("mobile_no"),
				"pass_type": "exhibitor_pass",
				"pass_code": r.get("pass_code") or r.get("registration_id"),
				"stall_number": r.get("stall_number"),
				"stall_category": r.get("stall_category"),
				"details": f"Stall {r.get('stall_number')} ({r.get('stall_category') or 'Exhibitor'}) - Pass 1 (Primary)",
				"checked_in": cint(r.get("checked_in")),
				"checked_in_at": str(r.get("checked_in_at")) if r.get("checked_in_at") else None,
				"checked_in_by": r.get("checked_in_by"),
				"status": "Checked In" if cint(r.get("checked_in")) else "Not Scanned",
				"creation": str(r.get("creation")),
			})
			# Pass 2: Co-Exhibitor Pass (Booth Staff)
			if r.get("pass_2_name"):
				records.append({
					"id": f"{r.get('registration_id') or r.get('name')}-P2",
					"name": f"{r.get('registration_id') or r.get('name')}-P2",
					"pass_index": 2,
					"pass_title": "Co-Exhibitor Pass (Booth Staff)",
					"full_name": r.get("pass_2_name"),
					"company_name": r.get("company_name"),
					"company_logo": format_full_url(r.get("company_logo")),
					"visitor_image": format_full_url(r.get("pass_2_image")),
					"email": r.get("pass_2_email") or r.get("email"),
					"mobile_no": r.get("pass_2_mobile") or r.get("mobile_no"),
					"pass_type": "exhibitor_pass",
					"pass_code": r.get("pass_2_pass_code") or f"{r.get('registration_id')}-P2",
					"stall_number": r.get("stall_number"),
					"stall_category": r.get("stall_category"),
					"details": f"Stall {r.get('stall_number')} ({r.get('stall_category') or 'Exhibitor'}) - Pass 2 (Co-Exhibitor)",
					"checked_in": cint(r.get("pass_2_checked_in")),
					"checked_in_at": str(r.get("pass_2_checked_in_at")) if r.get("pass_2_checked_in_at") else None,
					"checked_in_by": r.get("pass_2_checked_in_by"),
					"status": "Checked In" if cint(r.get("pass_2_checked_in")) else "Not Scanned",
					"creation": str(r.get("creation")),
				})

	# Sort by check-in time DESC or creation DESC
	records.sort(key=lambda x: x.get("checked_in_at") or x.get("creation") or "", reverse=True)

	total_matching = len(records)
	total_scanned = sum(1 for r in records if r.get("checked_in") == 1)
	total_not_scanned = total_matching - total_scanned

	# Summary stats across whole database
	vis_tot = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest Visitor Registration`")[0][0] if "visitor" in allowed_scopes else 0
	vis_chk = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest Visitor Registration` WHERE checked_in = 1")[0][0] if "visitor" in allowed_scopes else 0

	bni_tot = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest BNI Event Registration`")[0][0] if "event" in allowed_scopes else 0
	bni_chk = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest BNI Event Registration` WHERE checked_in = 1")[0][0] if "event" in allowed_scopes else 0

	if "exhibitor" in allowed_scopes:
		ex_p1_tot = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest Registration` WHERE payment_status = 'Paid'")[0][0]
		ex_p1_chk = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest Registration` WHERE payment_status = 'Paid' AND checked_in = 1")[0][0]
		ex_p2_tot = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest Registration` WHERE payment_status = 'Paid' AND pass_2_name IS NOT NULL AND pass_2_name != ''")[0][0]
		ex_p2_chk = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest Registration` WHERE payment_status = 'Paid' AND pass_2_checked_in = 1")[0][0]
		ex_tot = ex_p1_tot + ex_p2_tot
		ex_chk = ex_p1_chk + ex_p2_chk
	else:
		ex_tot = 0
		ex_chk = 0

	# Paginate
	start = (p - 1) * l
	end = start + l
	paginated = records[start:end]

	return {
		"status": "success",
		"summary": {
			"total_registered": vis_tot + bni_tot + ex_tot,
			"total_scanned": vis_chk + bni_chk + ex_chk,
			"total_not_scanned": max(0, (vis_tot + bni_tot + ex_tot) - (vis_chk + bni_chk + ex_chk)),
			"visitor_registered": vis_tot,
			"visitor_scanned": vis_chk,
			"visitor_not_scanned": max(0, vis_tot - vis_chk),
			"bni_event_registered": bni_tot,
			"bni_event_scanned": bni_chk,
			"bni_event_not_scanned": max(0, bni_tot - bni_chk),
			"exhibitor_registered": ex_tot,
			"exhibitor_scanned": ex_chk,
			"exhibitor_not_scanned": max(0, ex_tot - ex_chk),
			"current_filter": {
				"status": filter_status or "all",
				"type": filter_type or "all",
				"search": search_q or None,
				"scanned_by": scanner_filter or None,
			},
		},
		"pagination": {
			"page": p,
			"limit": l,
			"total_matching": total_matching,
			"total_pages": max(1, (total_matching + l - 1) // l),
		},
		"scanned_users": paginated,
	}


# Aliases
admin_get_scanned_users = get_scanned_users
get_check_in_list = get_scanned_users
admin_get_check_in_list = get_scanned_users


def get_registration(registration_id):
	"""Universal lookup by Registration ID for both Stalls and Visitors."""
	from connectfest.services.auth import resolve_authenticated_session_user, is_admin_user
	sess_user, _ = resolve_authenticated_session_user(registration_id=registration_id)
	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorized", "message": "Authentication required."}

	if not registration_id:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	is_admin = is_admin_user(sess_user)

	cache = frappe.cache()
	result = None

	# 1. Check Visitor Cache
	v_cached = get_cached_record(cache, f"{VISITOR_CACHE_PREFIX}{registration_id}")
	if v_cached:
		result = {"status": "success", "type": "visitor", "source": "cache", "data": v_cached}

	# 2. Check Stall Cache
	if not result:
		s_cached = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{registration_id}")
		if s_cached:
			result = {"status": "success", "type": "stall", "source": "cache", "data": s_cached}

	# 3. Check Database: Visitor
	if not result:
		v_records = frappe.get_all(
			"ConnectFest Visitor Registration",
			filters={"registration_id": registration_id},
			fields=["*"],
			limit=1,
		)
		if v_records:
			result = {"status": "success", "type": "visitor", "source": "db", "data": v_records[0]}

	# 4. Check Database: Stall
	if not result:
		s_records = frappe.get_all(
			"ConnectFest Registration",
			filters={"registration_id": registration_id},
			fields=["*"],
			limit=1,
		)
		if s_records:
			result = {"status": "success", "type": "stall", "source": "db", "data": s_records[0]}

	if not result:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": "Registration not found"}

	# IDOR Check
	if not is_admin:
		res_data = result.get("data", {})
		rec_email = (res_data.get("email") or "").strip().lower()
		rec_mob = (res_data.get("mobile_no") or "").strip()
		sess_lower = sess_user.strip().lower()
		user_phone = (frappe.db.get_value("User", sess_user, "phone") or frappe.db.get_value("User", sess_user, "mobile_no") or "").strip()
		if rec_email != sess_lower and (not user_phone or user_phone != rec_mob):
			frappe.local.response["http_status_code"] = 403
			return {"status": "forbidden", "message": "Access Denied: You cannot view another attendee's registration."}

	if result and "data" in result:
		result["full_name"] = result["data"].get("full_name")

	return result


def get_my_dashboard(email=None, mobile_no=None, security_pin=None, registration_id=None, **kwargs):
	"""
	Attendee Dashboard: Returns booked stall details (stall number, tier, dimensions, zone, receipt, exhibitor badge).
	Strictly requires authentication. Prevents IDOR peeking.
	"""
	from connectfest.services.common import mask_phone, mask_email, check_rate_limit
	from connectfest.services.auth import is_admin_user
	try:
		if not check_rate_limit("dashboard", limit=30, window_secs=60):
			frappe.local.response["http_status_code"] = 429
			return {"status": "error", "message": "Too many requests. Please wait a minute before trying again."}

		data = extract_request_data(kwargs)
		em = (email or data.get("email") or data.get("user_email") or "").strip().lower()
		mb = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()
		pin = (security_pin or data.get("security_pin") or data.get("pin") or "").strip()
		req_reg_id = (registration_id or data.get("registration_id") or "").strip()

		from connectfest.services.auth import resolve_authenticated_session_user
		sess_user, _ = resolve_authenticated_session_user(**kwargs)
		is_admin = is_admin_user(sess_user) if sess_user and sess_user != "Guest" else False

		if not sess_user or sess_user == "Guest":
			frappe.local.response["http_status_code"] = 401
			return {
				"status": "unauthorized",
				"message": "Authentication required.",
			}
		else:
			if not is_admin:
				if em and em != sess_user.lower():
					frappe.local.response["http_status_code"] = 403
					return {"status": "forbidden", "message": "Access Denied: You cannot view another attendee's dashboard or pass."}
				em = sess_user.lower()
			if not em and not mb and not req_reg_id:
				em = sess_user.lower()

		# 1. Fetch Confirmed Stall Booking
		stall_booking = None
		where_clauses = []
		params = {}
		if req_reg_id:
			where_clauses.append("registration_id = %(reg_id)s")
			params["reg_id"] = req_reg_id
		if em:
			where_clauses.append("email = %(email)s")
			params["email"] = em
		if mb:
			where_clauses.append("mobile_no = %(mobile)s")
			params["mobile"] = mb

		where_str = " OR ".join(where_clauses) if where_clauses else "1=0"
		has_exh_pass_col = frappe.db.has_column("ConnectFest Registration", "exhibitor_pass_url") if hasattr(frappe, "db") and frappe.db else True
		exh_pass_col_sql = ", exhibitor_pass_url, pass_2_pass_url" if has_exh_pass_col else ""

		stalls = frappe.db.sql(f"""
			SELECT registration_id, full_name, company_name, company_logo, location,
			       mobile_no, email, is_bni_member, region_name, chapter_name,
			       industry, segment, stall_number, stall_category, amount, payment_status,
			       payment_mode, razorpay_order_id, razorpay_payment_id, payment_date, status, creation,
			       bill, gst, pass_code, qr_payload, checked_in, checked_in_at, checked_in_by,
			       pass_1_image, pass_2_name, pass_2_image, pass_2_mobile, pass_2_email,
			       pass_2_pass_code, pass_2_qr_payload, pass_2_checked_in, pass_2_checked_in_at, pass_2_checked_in_by
			       {exh_pass_col_sql}
			FROM `tabConnectFest Registration`
			WHERE ({where_str})
			ORDER BY creation DESC
		""", params, as_dict=True)

		# 2. Fetch Visitor Passes (supports multiple passes for stall buyers)
		visitor_pass = None
		all_visitor_passes = []
		visitors = frappe.db.sql(f"""
			SELECT registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code,
			       qr_payload, status, checked_in, checked_in_at, creation, area_of_interest
			FROM `tabConnectFest Visitor Registration`
			WHERE ({where_str})
			ORDER BY creation DESC
		""", params, as_dict=True)

		if not stalls and not visitors:
			frappe.local.response["http_status_code"] = 404
			return {"status": "not_found", "message": "No registration found for the provided details. Please register first."}

		# Verify ownership security (Anti-IDOR)
		matched_reg = stalls[0] if stalls else (visitors[0] if visitors else None)
		if sess_user and sess_user != "Guest" and not is_admin and matched_reg:
			rec_email = (matched_reg.get("email") or "").strip().lower()
			rec_mob = (matched_reg.get("mobile_no") or "").strip()
			user_phone = (frappe.db.get_value("User", sess_user, "phone") or frappe.db.get_value("User", sess_user, "mobile_no") or "").strip()
			if rec_email != sess_user.lower() and (not user_phone or user_phone != rec_mob):
				frappe.local.response["http_status_code"] = 403
				return {"status": "forbidden", "message": "Access Denied: You cannot view another attendee's dashboard or pass."}

		is_authorized = True

		all_stall_bookings = []
		if stalls:
			for sb in stalls:
				if not sb.stall_number:
					continue

				stall_doc = None
				if frappe.db.exists("ConnectFest Stall", sb.stall_number):
					stall_doc = frappe.get_doc("ConnectFest Stall", sb.stall_number)

				zone = "Zone B - West Aisles"
				tier_name = sb.stall_category or (stall_doc.tier if stall_doc else "")
				if tier_name == "Silver":
					zone = "Zone D - East Pavilions"
				elif tier_name == "Premium":
					zone = "Zone C - Center Aisles"
				elif tier_name == "Standard":
					zone = "Zone B - West Aisles"
				elif tier_name == "Regular":
					zone = "Zone E - South Entrance Corridor"
				else:
					zone = None

				is_paid = (sb.payment_status == "Paid")
				status_desc = "Confirmed" if is_paid else ("Awaiting Payment" if sb.stall_number else "Stall Not Selected")

				raw_fn = (sb.full_name or "").strip()
				clean_fn = raw_fn.split("@")[0].replace(".", " ").replace("_", " ").title() if "@" in raw_fn else raw_fn

				all_stall_bookings.append({
					"registration_id": sb.registration_id,
					"full_name": clean_fn,
					"company_name": sb.company_name,
					"company_logo": format_full_url(sb.company_logo),
					"location": sb.location or "",
					"bill": sb.get("bill") or "",
					"gst": sb.get("gst") or "",
					"industry": sb.industry or "",
					"segment": sb.segment or "",
					"is_bni_member": bool(sb.is_bni_member),
					"region_name": sb.region_name or "",
					"chapter_name": sb.chapter_name or "",
					"email": sb.email if is_authorized else mask_email(sb.email),
					"mobile_no": sb.mobile_no if is_authorized else mask_phone(sb.mobile_no),
					"stall_number": sb.stall_number,
					"stall_category": tier_name,
					"zone": zone,
					"dimensions": (stall_doc.dimensions if stall_doc else "") or ("Standard" if sb.stall_number else None),
					"amount": flt(sb.amount),
					"payment_status": sb.payment_status,
					"payment_mode": sb.get("payment_mode") or ("Cash" if (sb.get("razorpay_payment_id") or "").startswith("CASH") else "Online"),
					"has_paid": is_paid,
					"has_selected_stall": bool(sb.stall_number),
					"next_action": "view_badge" if is_paid else ("proceed_to_payment" if sb.stall_number else "select_stall"),
					"razorpay_payment_id": sb.razorpay_payment_id if is_authorized else "pay_verified_secure",
					"razorpay_order_id": sb.razorpay_order_id if is_authorized else "order_verified_secure",
					"payment_date": str(sb.payment_date or sb.creation) if is_paid else None,
					"status": status_desc,
					"pass_code": sb.get("pass_code") or sb.registration_id,
					"badge_qr_payload": (sb.get("qr_payload") or f"CONNECTFEST2027:EXHIBITOR:{sb.stall_number}:{sb.registration_id}:{sb.email}") if is_paid else None,
					"qr_payload": (sb.get("qr_payload") or f"CONNECTFEST2027:EXHIBITOR:{sb.stall_number}:{sb.registration_id}:{sb.email}") if is_paid else None,
					"checked_in": cint(sb.get("checked_in") or 0),
					"checked_in_at": str(sb.get("checked_in_at") or "") if cint(sb.get("checked_in")) else None,
					"checked_in_by": sb.get("checked_in_by") or None,
					"pass_1_image": format_full_url(sb.get("pass_1_image")),
					"pass_2_name": sb.get("pass_2_name") or "",
					"pass_2_image": format_full_url(sb.get("pass_2_image")),
					"pass_2_mobile": sb.get("pass_2_mobile") or "",
					"pass_2_email": sb.get("pass_2_email") or "",
					"pass_2_pass_code": sb.get("pass_2_pass_code") or f"{sb.registration_id}-P2",
					"pass_2_qr_payload": sb.get("pass_2_qr_payload") if is_paid else None,
					"pass_2_checked_in": cint(sb.get("pass_2_checked_in") or 0),
					"pass_2_checked_in_at": str(sb.get("pass_2_checked_in_at") or "") if cint(sb.get("pass_2_checked_in")) else None,
					"pass_2_checked_in_by": sb.get("pass_2_checked_in_by") or None,
					"venue": "Chennai Trade Centre, Nandambakkam",
					"dates": "20 & 21 Feb 2027",
					"exhibitor_pass_url": format_full_url(sb.get("exhibitor_pass_url")),
				})

		exhibitor_passes = []
		for sb in all_stall_bookings:
			if sb.get("has_paid") and sb.get("stall_number"):
				chk_e = cint(sb.get("checked_in") or 0)
				# Pass 1: Primary Exhibitor Pass
				p1 = {
					"pass_index": 1,
					"pass_number": 1,
					"pass_title": "Primary Exhibitor Pass",
					"pass_type": "exhibitor_pass",
					"pass_code": sb.get("pass_code") or sb.get("registration_id"),
					"registration_id": sb.get("registration_id"),
					"full_name": sb.get("full_name"),
					"company_name": sb.get("company_name"),
					"company_logo": sb.get("company_logo"),
					"visitor_image": format_full_url(sb.get("pass_1_image") or sb.get("company_logo")),
					"image": format_full_url(sb.get("pass_1_image") or sb.get("company_logo")),
					"exhibitor_pass_url": format_full_url(sb.get("exhibitor_pass_url")),
					"location": sb.get("location") or "",
					"stall_number": sb.get("stall_number"),
					"stall_category": sb.get("stall_category"),
					"zone": sb.get("zone"),
					"dimensions": sb.get("dimensions"),
					"access": "EXHIBITOR - BOTH DAYS & SETUP",
					"which_day": "Both days",
					"payment_status": "Paid",
					"payment_mode": sb.get("payment_mode"),
					"qr_payload": sb.get("qr_payload"),
					"is_assigned": True,
					"status": "Checked In" if chk_e else "Confirmed",
					"checked_in": chk_e,
					"is_attended": bool(chk_e),
					"checked_in_at": sb.get("checked_in_at"),
					"checked_in_by": sb.get("checked_in_by"),
					"venue": "Chennai Trade Centre, Nandambakkam",
					"dates": "20 & 21 Feb 2027",
				}
				exhibitor_passes.append(p1)

				# Pass 2: Co-Exhibitor Pass (Booth Staff)
				chk_p2 = cint(sb.get("pass_2_checked_in") or 0)
				p2_name = (sb.get("pass_2_name") or "").strip()
				is_p2_assigned = bool(p2_name)
				p2_code = (sb.get("pass_2_pass_code") or "").strip() if is_p2_assigned else None
				if is_p2_assigned and not p2_code:
					p2_code = f"{sb.get('registration_id')}-P2"
				p2_qr = (sb.get("pass_2_qr_payload") or "").strip() if is_p2_assigned else None
				if is_p2_assigned and not p2_qr and p2_code:
					p2_qr = f"CONNECTFEST2027:EXHIBITOR:{sb.get('stall_number')}:{p2_code}:{sb.get('pass_2_email') or sb.get('email')}"
				p2 = {
					"pass_index": 2,
					"pass_number": 2,
					"pass_title": "Co-Exhibitor Pass (Booth Staff)",
					"pass_type": "exhibitor_pass",
					"pass_code": p2_code,
					"registration_id": sb.get("registration_id"),
					"full_name": p2_name or "Co-Exhibitor (Unassigned)",
					"visitor_image": format_full_url(sb.get("pass_2_image")) if is_p2_assigned else "",
					"image": format_full_url(sb.get("pass_2_image")) if is_p2_assigned else "",
					"exhibitor_pass_url": format_full_url(sb.get("pass_2_pass_url")) if is_p2_assigned else "",
					"mobile_no": sb.get("pass_2_mobile") or "" if is_p2_assigned else "",
					"email": sb.get("pass_2_email") or "" if is_p2_assigned else "",
					"company_name": sb.get("company_name"),
					"company_logo": sb.get("company_logo"),
					"location": sb.get("location") or "",
					"stall_number": sb.get("stall_number"),
					"stall_category": sb.get("stall_category"),
					"zone": sb.get("zone"),
					"dimensions": sb.get("dimensions"),
					"access": "EXHIBITOR - BOTH DAYS & SETUP",
					"which_day": "Both days",
					"payment_status": "Paid",
					"payment_mode": sb.get("payment_mode"),
					"qr_payload": p2_qr,
					"is_assigned": is_p2_assigned,
					"status": "Checked In" if chk_p2 else ("Confirmed" if is_p2_assigned else "Unassigned"),
					"checked_in": chk_p2,
					"is_attended": bool(chk_p2),
					"checked_in_at": str(sb.get("pass_2_checked_in_at") or "") if chk_p2 else None,
					"checked_in_by": sb.get("pass_2_checked_in_by"),
					"venue": "Chennai Trade Centre, Nandambakkam",
					"dates": "20 & 21 Feb 2027",
				}
				exhibitor_passes.append(p2)
		exhibitor_pass = exhibitor_passes[0] if exhibitor_passes else None

		stall_booking = all_stall_bookings[0] if all_stall_bookings else None
		base_url = get_base_url()

		if visitors:
			for v in visitors:
				raw_v_fn = (v.full_name or "").strip()
				clean_v_fn = raw_v_fn.split("@")[0].replace(".", " ").replace("_", " ").title() if "@" in raw_v_fn else raw_v_fn
				all_visitor_passes.append({
					"registration_id": v.registration_id,
					"pass_code": v.pass_code,
					"pass_type": "visitor_pass",
					"pass_title": "Visitor Gate Pass",
					"full_name": clean_v_fn,
					"image": format_full_url(v.visitor_image),
					"visitor_image": format_full_url(v.visitor_image),
					"email": v.email if is_authorized else mask_email(v.email),
					"mobile_no": v.mobile_no if is_authorized else mask_phone(v.mobile_no),
					"which_day": v.which_day,
					"area_of_interest": v.get("area_of_interest") or "",
					"access": "VISITOR - BOTH DAYS" if (not v.which_day or v.which_day == "Both days") else f"VISITOR - {v.which_day.upper()}",
					"status": v.status,
					"checked_in": v.checked_in,
					"checked_in_at": str(v.checked_in_at or ""),
					"qr_payload": (v.qr_payload or f"CONNECTFEST2027:{v.pass_code}:{v.email}") if is_authorized else None,
					"venue": "Chennai Trade Centre, Nandambakkam",
					"dates": "20 & 21 Feb 2027",
					"qr_url": get_pass_qr_url(v.pass_code),
					"qr_download_url": f"{base_url}/api/method/connectfest.api.download_pass_qr?code={urllib.parse.quote(str(v.pass_code))}",
					"badge_download_url": f"{base_url}/api/method/connectfest.api.download_visitor_badge?code={urllib.parse.quote(str(v.pass_code))}",
				})
			visitor_pass = all_visitor_passes[0] if all_visitor_passes else None

		resolved_dashboard_full_name = None
		if exhibitor_pass and exhibitor_pass.get("full_name"):
			resolved_dashboard_full_name = exhibitor_pass.get("full_name")
		elif stall_booking and stall_booking.get("full_name"):
			resolved_dashboard_full_name = stall_booking.get("full_name")
		elif visitor_pass and visitor_pass.get("full_name"):
			resolved_dashboard_full_name = visitor_pass.get("full_name")

		res = {
			"status": "success",
			"full_name": resolved_dashboard_full_name,
			"is_authorized": is_authorized,
			"has_exhibitor_pass": bool(exhibitor_passes),
			"exhibitor_pass": exhibitor_pass,
			"exhibitor_passes": exhibitor_passes,
			"total_exhibitor_passes": len(exhibitor_passes),
			"max_exhibitor_passes_per_stall": 2,
			"pass_code": (
				exhibitor_passes[0]["pass_code"] if exhibitor_passes
				else (visitor_pass["pass_code"] if visitor_pass
				else None)
			),
			"qr_payload": (
				exhibitor_passes[0]["qr_payload"] if exhibitor_passes
				else (visitor_pass.get("qr_payload") if visitor_pass
				else None)
			),
			"has_stall_booking": bool(all_stall_bookings),
			"stall_booking": stall_booking,
			"stall_bookings": all_stall_bookings,
			"total_stalls": len(all_stall_bookings),
			"has_visitor_pass": bool(visitor_pass),
			"visitor_pass": visitor_pass,
			"visitor_passes": all_visitor_passes,
			"total_visitor_passes": len(all_visitor_passes),
		}

		if not is_authorized:
			res["security_notice"] = "🔒 Privacy Shield Active: Sensitive phone numbers and entry QR codes are protected. Provide your Registration ID to unlock full exhibitor badge and QR code."

		return res
	except Exception as e:
		return log_and_return_error("ConnectFest Dashboard Error", e)


def get_exhibitor_badge(registration_id=None, stall_number=None, **kwargs):
	"""
	Universal Exhibitor Badge / Digital Pass API.
	Returns verified exhibitor pass details with pass_code, stall info, zone, and entry QR payload.
	Supports both Pass 1 (Primary Exhibitor) and Pass 2 (Co-Exhibitor / Booth Staff).
	"""
	data = extract_request_data(kwargs)
	reg_id = (registration_id or data.get("registration_id") or data.get("id") or "").strip()
	stall_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip()
	em = safe_str(kwargs.get("email") or data.get("email") or data.get("user_email")).strip().lower()
	mb = safe_str(kwargs.get("mobile_no") or data.get("mobile_no") or data.get("phone")).strip()
	req_pass_index = cint(data.get("pass_index") or data.get("slot") or kwargs.get("pass_index") or 0)
	req_code = (data.get("pass_code") or kwargs.get("pass_code") or "").strip()

	from connectfest.services.auth import is_admin_user, resolve_authenticated_session_user
	auth_user, _ = resolve_authenticated_session_user(**kwargs)
	if not auth_user or auth_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorized", "message": "Authentication required."}
	is_admin = is_admin_user(auth_user)

	where_clauses = []
	params = {}
	if reg_id:
		clean_reg = reg_id.rsplit("-P2", 1)[0]
		where_clauses.append("(registration_id = %(reg_id)s OR name = %(reg_id)s OR pass_code = %(reg_id)s OR pass_2_pass_code = %(raw_reg)s OR registration_id = %(clean_reg)s)")
		params["reg_id"] = reg_id
		params["raw_reg"] = reg_id
		params["clean_reg"] = clean_reg
	if stall_num:
		where_clauses.append("stall_number = %(stall_num)s")
		params["stall_num"] = stall_num
	if em:
		where_clauses.append("(email = %(email)s OR pass_2_email = %(email)s)")
		params["email"] = em
	if mb:
		where_clauses.append("(mobile_no = %(mobile)s OR pass_2_mobile = %(mobile)s)")
		params["mobile"] = mb

	if not where_clauses:
		if auth_user and auth_user != "Guest":
			where_clauses.append("email = %(email)s")
			params["email"] = auth_user.lower()
		else:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "registration_id, stall_number, email, or login required to view exhibitor badge"}

	where_str = " OR ".join(where_clauses)
	has_exh_pass_col = frappe.db.has_column("ConnectFest Registration", "exhibitor_pass_url") if hasattr(frappe, "db") and frappe.db else True
	exh_pass_col_sql = ", exhibitor_pass_url, pass_2_pass_url" if has_exh_pass_col else ""
	records = frappe.db.sql(f"""
		SELECT name, registration_id, full_name, company_name, company_logo, location,
		       mobile_no, email, is_bni_member, region_name, chapter_name,
		       industry, segment, stall_number, stall_category, amount, payment_status,
		       payment_mode, payment_date, status, creation, pass_code, qr_payload,
		       checked_in, checked_in_at, checked_in_by, pass_1_image,
		       pass_2_name, pass_2_image, pass_2_mobile, pass_2_email,
		       pass_2_pass_code, pass_2_qr_payload, pass_2_checked_in, pass_2_checked_in_at, pass_2_checked_in_by
		       {exh_pass_col_sql}
		FROM `tabConnectFest Registration`
		WHERE ({where_str})
		ORDER BY creation DESC
	""", params, as_dict=True)

	if not records:
		frappe.local.response["http_status_code"] = 404
		return {"status": "not_found", "message": "No exhibitor registration found for the provided details."}

	rec = records[0]

	# IDOR Check: Ensure requester is owner or admin
	if auth_user and not is_admin:
		r_email = (rec.get("email") or "").strip().lower()
		r_mob = (rec.get("mobile_no") or "").strip()
		u_phone = (frappe.db.get_value("User", auth_user, "phone") or frappe.db.get_value("User", auth_user, "mobile_no") or "").strip()
		if r_email != auth_user.lower() and (not u_phone or u_phone != r_mob):
			frappe.local.response["http_status_code"] = 403
			return {"status": "forbidden", "message": "Access Denied: You cannot view another exhibitor's badge."}

	is_paid = (rec.get("payment_status") == "Paid")
	stall_doc = None
	if rec.get("stall_number") and frappe.db.exists("ConnectFest Stall", rec.get("stall_number")):
		stall_doc = frappe.get_doc("ConnectFest Stall", rec.get("stall_number"))

	zone_map = {
		"Silver": "Zone D - East Pavilions",
		"Premium": "Zone C - Center Aisles",
		"Standard": "Zone B - West Aisles",
		"Regular": "Zone E - South Entrance Corridor",
	}
	zone = zone_map.get(rec.get("stall_category")) or (stall_doc.zone if stall_doc else "Zone B - West Aisles")
	dimensions = (stall_doc.dimensions if stall_doc else "") or "3 x 3 m"

	base_url = "https://api.connectfesttn.in"
	try:
		from connectfest.services.notifications import get_base_url
		base_url = get_base_url()
	except Exception:
		pass

	# Ensure Pass 1 Badge Image
	raw_exh_pass = rec.get("exhibitor_pass_url") or ""
	if not raw_exh_pass:
		try:
			from connectfest.services.exhibitor_badge import generate_exhibitor_badge_image
			raw_exh_pass = generate_exhibitor_badge_image(rec, pass_index=1)
			if raw_exh_pass and frappe.db.has_column("ConnectFest Registration", "exhibitor_pass_url"):
				frappe.db.set_value("ConnectFest Registration", rec.get("name"), "exhibitor_pass_url", raw_exh_pass)
				frappe.db.commit()
		except Exception:
			pass
	badge_1_url = format_full_url(raw_exh_pass) if raw_exh_pass else ""
	dl_1_url = f"{base_url}/api/method/connectfest.api.download_exhibitor_badge?code={urllib.parse.quote(str(rec.get('pass_code') or rec.get('registration_id')))}"

	# Pass 1: Primary Exhibitor Pass
	pcode1 = rec.get("pass_code") or rec.get("registration_id")
	raw_qr1 = rec.get("qr_payload")
	qr1 = pcode1 if (not raw_qr1 or "CONNECTFEST" in str(raw_qr1)) else raw_qr1
	chk1 = cint(rec.get("checked_in") or 0)

	pass_1 = {
		"pass_index": 1,
		"pass_number": 1,
		"pass_title": "Primary Exhibitor Pass",
		"pass_type": "exhibitor_pass",
		"pass_code": pcode1,
		"registration_id": rec.get("registration_id") or rec.get("name"),
		"full_name": rec.get("full_name"),
		"company_name": rec.get("company_name"),
		"company_logo": format_full_url(rec.get("company_logo")),
		"visitor_image": format_full_url(rec.get("pass_1_image") or rec.get("company_logo")),
		"image": format_full_url(rec.get("pass_1_image") or rec.get("company_logo")),
		"exhibitor_pass_url": badge_1_url,
		"location": rec.get("location") or "",
		"stall_number": rec.get("stall_number"),
		"stall_category": rec.get("stall_category"),
		"tier": rec.get("stall_category"),
		"zone": zone,
		"dimensions": dimensions,
		"access": "EXHIBITOR - BOTH DAYS & SETUP",
		"which_day": "Both days",
		"payment_status": rec.get("payment_status"),
		"payment_mode": rec.get("payment_mode"),
		"has_paid": is_paid,
		"qr_payload": qr1 if is_paid else None,
		"qr_url": get_pass_qr_url(pcode1) if is_paid else None,
		"qr_download_url": f"{base_url}/api/method/connectfest.api.download_pass_qr?code={urllib.parse.quote(str(pcode1))}" if is_paid else None,
		"is_assigned": True,
		"status": "Checked In" if chk1 else ("Confirmed" if is_paid else "Awaiting Payment"),
		"checked_in": chk1,
		"is_attended": bool(chk1),
		"checked_in_at": str(rec.get("checked_in_at") or "") if chk1 else None,
		"checked_in_by": rec.get("checked_in_by"),
		"venue": "Chennai Trade Centre, Nandambakkam",
		"dates": "20 & 21 Feb 2027",
	}

	# Pass 2: Co-Exhibitor Pass (Booth Staff)
	p2_name = (rec.get("pass_2_name") or "").strip()
	is_p2_assigned = bool(p2_name)
	pcode2 = (rec.get("pass_2_pass_code") or "").strip() if is_p2_assigned else None
	if is_p2_assigned and not pcode2:
		pcode2 = f"{rec.get('registration_id')}-P2"
	raw_qr2 = rec.get("pass_2_qr_payload")
	qr2 = pcode2 if (not raw_qr2 or "CONNECTFEST" in str(raw_qr2)) else raw_qr2
	chk2 = cint(rec.get("pass_2_checked_in") or 0)

	# Ensure Pass 2 Badge Image
	raw_p2_pass = rec.get("pass_2_pass_url") or ""
	if is_p2_assigned and not raw_p2_pass:
		try:
			from connectfest.services.exhibitor_badge import generate_exhibitor_badge_image
			raw_p2_pass = generate_exhibitor_badge_image(rec, pass_index=2)
			if raw_p2_pass and frappe.db.has_column("ConnectFest Registration", "pass_2_pass_url"):
				frappe.db.set_value("ConnectFest Registration", rec.get("name"), "pass_2_pass_url", raw_p2_pass)
				frappe.db.commit()
		except Exception:
			pass
	badge_2_url = format_full_url(raw_p2_pass) if (is_p2_assigned and raw_p2_pass) else ""
	dl_2_url = f"{base_url}/api/method/connectfest.api.download_exhibitor_badge?code={urllib.parse.quote(str(pcode2))}&pass_index=2" if is_p2_assigned else ""

	pass_2 = {
		"pass_index": 2,
		"pass_number": 2,
		"pass_title": "Co-Exhibitor Pass (Booth Staff)",
		"pass_type": "exhibitor_pass",
		"pass_code": pcode2,
		"registration_id": rec.get("registration_id") or rec.get("name"),
		"full_name": p2_name or "Co-Exhibitor (Unassigned)",
		"visitor_image": format_full_url(rec.get("pass_2_image")) if is_p2_assigned else "",
		"image": format_full_url(rec.get("pass_2_image")) if is_p2_assigned else "",
		"exhibitor_pass_url": badge_2_url,
		"mobile_no": rec.get("pass_2_mobile") or "" if is_p2_assigned else "",
		"email": rec.get("pass_2_email") or "" if is_p2_assigned else "",
		"company_name": rec.get("company_name"),
		"company_logo": format_full_url(rec.get("company_logo")),
		"location": rec.get("location") or "",
		"stall_number": rec.get("stall_number"),
		"stall_category": rec.get("stall_category"),
		"tier": rec.get("stall_category"),
		"zone": zone,
		"dimensions": dimensions,
		"access": "EXHIBITOR - BOTH DAYS & SETUP",
		"which_day": "Both days",
		"payment_status": rec.get("payment_status"),
		"payment_mode": rec.get("payment_mode"),
		"has_paid": is_paid,
		"qr_payload": qr2 if is_paid else None,
		"qr_url": get_pass_qr_url(pcode2) if (is_paid and pcode2) else None,
		"qr_download_url": f"{base_url}/api/method/connectfest.api.download_pass_qr?code={urllib.parse.quote(str(pcode2))}" if (is_paid and pcode2) else None,
		"is_assigned": is_p2_assigned,
		"status": "Checked In" if chk2 else ("Confirmed" if is_p2_assigned else "Unassigned"),
		"checked_in": chk2,
		"is_attended": bool(chk2),
		"checked_in_at": str(rec.get("pass_2_checked_in_at") or "") if chk2 else None,
		"checked_in_by": rec.get("pass_2_checked_in_by"),
		"venue": "Chennai Trade Centre, Nandambakkam",
		"dates": "20 & 21 Feb 2027",
	}

	selected_pass = pass_1
	if req_pass_index == 2 or (req_code and (req_code == pcode2 or "-P2" in req_code.upper() or req_code.endswith("-2"))):
		selected_pass = pass_2

	return {
		"status": "success",
		"badge": selected_pass,
		"pass": selected_pass,
		"exhibitor_pass": selected_pass,
		"passes": [pass_1, pass_2],
		"total_passes": 2,
		"max_passes": 2,
	}


def update_co_exhibitor_pass(registration_id=None, stall_number=None, name=None, full_name=None, image=None, photo=None, mobile_no=None, email=None, **kwargs):
	"""
	Configures or updates the 2nd Exhibitor Pass (Friend / Booth Staff) for a confirmed booked stall.
	- Each stall booking gets strictly maximum 2 exhibitor passes (1 Primary + 1 Co-Exhibitor).
	- Restricted to the attendee who owns the stall registration or Administrator.
	- Requires confirmed payment (Paid).
	- Saves friend/staff member's photo and generates secure QR pass payload.
	"""
	from connectfest.services.auth import is_admin_user, resolve_authenticated_session_user
	from connectfest.services.common import save_image_from_base64_or_url
	data = extract_request_data(kwargs)

	auth_user, _ = resolve_authenticated_session_user(**kwargs)
	if not auth_user or auth_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorized", "message": "Authentication required."}

	is_admin = is_admin_user(auth_user)

	p2_name = (name or full_name or data.get("name") or data.get("full_name") or data.get("co_exhibitor_name") or data.get("second_person_name") or data.get("friend_name") or "").strip()
	p2_mobile = (mobile_no or data.get("mobile_no") or data.get("mobile") or data.get("phone") or "").strip()
	p2_email = safe_str(email or data.get("email") or data.get("mail")).strip().lower()

	raw_img = image or photo or data.get("image") or data.get("photo") or data.get("visitor_image") or data.get("avatar") or data.get("file") or data.get("filedata")

	target_reg = (registration_id or data.get("registration_id") or "").strip()
	target_stall = (stall_number or data.get("stall_number") or "").strip()

	if not p2_name:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Co-Exhibitor full name (name/full_name) is mandatory for the 2nd pass holder.",
		}

	# Locate registration
	where_conds = []
	params = {}
	if target_reg:
		where_conds.append("(registration_id = %(reg_id)s OR name = %(reg_id)s)")
		params["reg_id"] = target_reg
	elif target_stall:
		where_conds.append("stall_number = %(stall)s")
		params["stall"] = target_stall
	elif auth_user and auth_user != "Guest":
		where_conds.append("email = %(auth)s")
		params["auth"] = auth_user.lower()
	else:
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorized", "message": "Authentication required. Please log in or provide registration_id."}

	where_sql = " OR ".join(where_conds)
	records = frappe.db.sql(f"""
		SELECT name, registration_id, full_name, company_name, company_logo, location,
		       mobile_no, email, stall_number, stall_category, payment_status,
		       pass_code, pass_2_name, pass_2_image, pass_2_mobile, pass_2_email,
		       pass_2_pass_code, pass_2_qr_payload, pass_2_checked_in
		FROM `tabConnectFest Registration`
		WHERE ({where_sql})
		ORDER BY creation DESC
	""", params, as_dict=True)

	if not records:
		frappe.local.response["http_status_code"] = 404
		return {"status": "not_found", "message": "No stall registration found for the provided details."}

	rec = records[0]

	# IDOR Check: Ensure caller owns this registration (unless Admin)
	if auth_user and not is_admin:
		rec_email = (rec.get("email") or "").strip().lower()
		rec_mob = (rec.get("mobile_no") or "").strip()
		u_phone = (frappe.db.get_value("User", auth_user, "phone") or frappe.db.get_value("User", auth_user, "mobile_no") or "").strip()
		if rec_email != auth_user.lower() and (not u_phone or u_phone != rec_mob):
			frappe.local.response["http_status_code"] = 403
			return {"status": "forbidden", "message": "Access Denied: You cannot configure passes for another exhibitor."}

	# Check stall allocation & payment
	if not rec.get("stall_number"):
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Stall selection is required before generating exhibitor passes."}

	if rec.get("payment_status") != "Paid":
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "payment_pending",
			"message": f"Stall '{rec.stall_number}' payment is pending ({rec.payment_status or 'Pending'}). Co-exhibitor pass can only be configured after payment confirmation.",
		}

	# Save uploaded image
	saved_img = save_image_from_base64_or_url(raw_img, file_name_prefix=f"exh2_{rec.stall_number}") if raw_img else (rec.get("pass_2_image") or "")

	p2_code = (rec.get("pass_2_pass_code") or "").strip()
	if not p2_code or p2_code.startswith("CF-2611-") or p2_code.endswith("-P2"):
		from connectfest.services.common import generate_secure_pass_code
		p2_code = generate_secure_pass_code("CF")
	p2_qr = p2_code

	frappe.db.set_value("ConnectFest Registration", rec.name, {
		"pass_2_name": p2_name,
		"pass_2_image": saved_img,
		"pass_2_mobile": p2_mobile,
		"pass_2_email": p2_email,
		"pass_2_pass_code": p2_code,
		"pass_2_qr_payload": p2_qr,
	})
	frappe.db.commit()

	zone_map = {
		"Silver": "Zone D - East Pavilions",
		"Premium": "Zone C - Center Aisles",
		"Standard": "Zone B - West Aisles",
		"Regular": "Zone E - South Entrance Corridor",
	}
	zone = zone_map.get(rec.stall_category) or "Zone B - West Aisles"

	pass_2_data = {
		"pass_index": 2,
		"pass_title": "Co-Exhibitor Pass (Booth Staff)",
		"pass_type": "exhibitor_pass",
		"pass_code": p2_code,
		"registration_id": rec.registration_id or rec.name,
		"full_name": p2_name,
		"image": format_full_url(saved_img),
		"mobile_no": p2_mobile,
		"email": p2_email,
		"company_name": rec.company_name,
		"stall_number": rec.stall_number,
		"stall_category": rec.stall_category,
		"zone": zone,
		"access": "EXHIBITOR - ALL AREAS & SETUP",
		"which_day": "Both days",
		"payment_status": "Paid",
		"qr_payload": p2_qr,
		"is_assigned": True,
		"checked_in": cint(rec.get("pass_2_checked_in") or 0),
		"venue": "Chennai Trade Centre, Nandambakkam",
		"dates": "20 & 21 Feb 2027",
	}

	return {
		"status": "success",
		"message": f"Co-Exhibitor Pass 2 successfully issued to {p2_name} for Stall {rec.stall_number}!",
		"pass": pass_2_data,
	}


# Aliases
update_second_exhibitor_pass = update_co_exhibitor_pass
set_second_exhibitor_pass = update_co_exhibitor_pass
assign_co_exhibitor_pass = update_co_exhibitor_pass


def generate_local_qr_png_bytes(data_str, scale=10, border=2):
	"""
	Generates a crisp, high-contrast QR code PNG entirely in-memory using pyqrcode and Pillow.
	Completely local: zero external API calls, zero rate-limits, works 100% offline.
	"""
	import pyqrcode
	from PIL import Image, ImageDraw

	qr = pyqrcode.create(str(data_str))
	matrix = qr.code
	size = (len(matrix) + 2 * border) * scale
	img = Image.new("RGB", (size, size), "white")
	draw = ImageDraw.Draw(img)

	for r_idx, row in enumerate(matrix):
		for c_idx, val in enumerate(row):
			if val == 1:
				x0 = (c_idx + border) * scale
				y0 = (r_idx + border) * scale
				draw.rectangle([x0, y0, x0 + scale, y0 + scale], fill="black")

	buf = io.BytesIO()
	img.save(buf, format="PNG", optimize=True)
	return buf.getvalue()


def get_pass_qr_url(code_str):
	"""
	Returns a reliable public HTTPS QR code image URL for emails and web.
	Also generates and saves the local PNG file to public files for downloads.
	Uses QuickChart (Cloudflare Global CDN) to guarantee instant rendering in Gmail proxy,
	Outlook SafeLinks, Yahoo, Apple Mail, and mobile inboxes without firewall or localhost issues.
	"""
	clean_code = str(code_str or "").strip()
	if not clean_code:
		clean_code = "CONNECTFEST-GATE-PASS"

	safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", clean_code)
	filename = f"ConnectFest_Pass_QR_{safe_code}.png"

	# Save local PNG file for downloadable attachment / direct download
	try:
		public_dir = frappe.get_site_path("public", "files")
		filepath = os.path.join(public_dir, filename)
		if not os.path.exists(filepath):
			png_bytes = generate_local_qr_png_bytes(clean_code, scale=8, border=2)
			with open(filepath, "wb") as f:
				f.write(png_bytes)
	except Exception:
		pass

	return f"https://quickchart.io/qr?text={urllib.parse.quote(clean_code)}&size=180&margin=2"


def get_pass_qr_image(code=None, pass_code=None, registration_id=None, pass_number=1, **kwargs):
	"""
	Public inline image API returning the Gate Pass QR Code directly as image/png.
	Cache-friendly so email clients and web apps display it instantly.
	"""
	req_data = extract_request_data(kwargs)
	clean_code = (code or pass_code or req_data.get("code") or req_data.get("pass_code") or "").strip()
	reg_id = (registration_id or req_data.get("registration_id") or "").strip()

	if not clean_code and reg_id:
		p_num = cint(pass_number or req_data.get("pass_number") or 1)
		field = "pass_2_pass_code" if p_num == 2 else "pass_code"
		clean_code = frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, field) or ""
		if not clean_code and p_num == 1:
			clean_code = frappe.db.get_value("ConnectFest Visitor Registration", {"registration_id": reg_id}, "pass_code") or ""
		if not clean_code:
			clean_code = reg_id

	if not clean_code:
		clean_code = "CONNECTFEST-GATE-PASS"

	safe_filename_code = re.sub(r"[^A-Za-z0-9_-]", "_", clean_code)
	filename = f"ConnectFest_Pass_QR_{safe_filename_code}.png"

	png_bytes = None
	try:
		public_dir = frappe.get_site_path("public", "files")
		filepath = os.path.join(public_dir, filename)
		if os.path.exists(filepath):
			with open(filepath, "rb") as f:
				png_bytes = f.read()
	except Exception:
		pass

	if not png_bytes:
		png_bytes = generate_local_qr_png_bytes(clean_code, scale=10, border=2)

	frappe.local.response.filename = filename
	frappe.local.response.filecontent = png_bytes
	frappe.local.response.type = "binary"
	frappe.local.response.content_type = "image/png"
	frappe.local.response.headers = {
		"Cache-Control": "public, max-age=31536000",
		"Content-Disposition": f'inline; filename="{filename}"'
	}


def generate_pass_card_png_bytes(code_str, pass_type='EXHIBITOR PASS', name='', subtitle='', dates='20 & 21 Feb 2027 • Rajapalayam'):
	"""
	Generates a high-resolution, branded digital Gate Pass Card PNG image.
	Perfect vertical card layout with:
	  - Event Title Header: CONNECTFEST 2027
	  - Category Banner: EXHIBITOR PASS / VISITOR PASS / BNI EVENT PASS
	  - Attendee Name & Subtitle (Stall / Access / Chapter info)
	  - High-contrast, scannable QR Code container
	  - Clean Pass Code box (e.g. CF-XXXX-XXXX)
	  - Scan instruction & Event Schedule
	  - Bottom footer bar
	"""
	import io
	import pyqrcode
	from PIL import Image, ImageDraw, ImageFont

	W, H = 480, 640
	img = Image.new('RGB', (W, H), '#ffffff')
	draw = ImageDraw.Draw(img)

	try:
		font_tag = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 12)
		font_title = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 20)
		font_name = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 20)
		font_sub = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 13)
		font_code = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 17)
		font_scan = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 11)
		font_info = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 12)
		font_footer = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 10)
	except Exception:
		font_tag = font_title = font_name = font_sub = font_code = font_scan = font_info = font_footer = ImageFont.load_default()

	is_visitor = 'VISITOR' in (pass_type or '').upper()
	is_event = 'EVENT' in (pass_type or '').upper() or 'BNI' in (pass_type or '').upper()

	if is_visitor:
		header_bg = '#b91c1c'
		tag_color = '#fecaca'
		badge_bg = '#fef2f2'
		badge_border = '#fca5a5'
		code_color = '#b91c1c'
	elif is_event:
		header_bg = '#0f766e'
		tag_color = '#ccfbf1'
		badge_bg = '#f0fdfa'
		badge_border = '#99f6e4'
		code_color = '#0f766e'
	else:
		header_bg = '#4338ca'
		tag_color = '#c7d2fe'
		badge_bg = '#eef2ff'
		badge_border = '#c7d2fe'
		code_color = '#4338ca'

	# Top header banner (105px height)
	draw.rectangle([0, 0, W, 105], fill=header_bg)

	tag_text = '✦  CONNECTFEST 2027  ✦'
	bbox_tag = draw.textbbox((0, 0), tag_text, font=font_tag)
	draw.text(((W - (bbox_tag[2] - bbox_tag[0])) / 2, 22), tag_text, fill=tag_color, font=font_tag)

	title_text = pass_type or ('VISITOR PASS' if is_visitor else ('BNI EVENT PASS' if is_event else 'EXHIBITOR PASS'))
	bbox_title = draw.textbbox((0, 0), title_text, font=font_title)
	draw.text(((W - (bbox_title[2] - bbox_title[0])) / 2, 50), title_text, fill='#ffffff', font=font_title)

	# Attendee Info Section
	curr_y = 125
	name_display = name or 'Valued Attendee'
	bbox_name = draw.textbbox((0, 0), name_display, font=font_name)
	draw.text(((W - (bbox_name[2] - bbox_name[0])) / 2, curr_y), name_display, fill='#0f172a', font=font_name)
	curr_y += 30

	sub_display = subtitle or 'Official Digital Pass'
	bbox_sub = draw.textbbox((0, 0), sub_display, font=font_sub)
	draw.text(((W - (bbox_sub[2] - bbox_sub[0])) / 2, curr_y), sub_display, fill='#475569', font=font_sub)
	curr_y += 28

	# Subtle separator line
	draw.line([(35, curr_y), (W - 35, curr_y)], fill='#f1f5f9', width=1)
	curr_y += 12

	# Generate crisp local QR code
	qr = pyqrcode.create(str(code_str))
	matrix = qr.code
	scale = 7
	border = 2
	qr_size = (len(matrix) + 2 * border) * scale
	qr_img = Image.new('RGB', (qr_size, qr_size), '#ffffff')
	qr_draw = ImageDraw.Draw(qr_img)
	for r_idx, row in enumerate(matrix):
		for c_idx, val in enumerate(row):
			if val == 1:
				x0 = (c_idx + border) * scale
				y0 = (r_idx + border) * scale
				qr_draw.rectangle([x0, y0, x0 + scale, y0 + scale], fill='black')

	box_padding = 10
	qr_box_size = qr_size + box_padding * 2
	qr_box_x = int((W - qr_box_size) / 2)
	qr_box_y = curr_y
	draw.rounded_rectangle([qr_box_x, qr_box_y, qr_box_x + qr_box_size, qr_box_y + qr_box_size], radius=8, fill='#ffffff', outline='#e2e8f0', width=1)
	img.paste(qr_img, (qr_box_x + box_padding, qr_box_y + box_padding))
	curr_y += qr_box_size + 14

	# Prominent Pass Code badge box
	code_box_w = 360
	code_box_h = 44
	code_box_x = int((W - code_box_w) / 2)
	draw.rounded_rectangle([code_box_x, curr_y, code_box_x + code_box_w, curr_y + code_box_h], radius=6, fill=badge_bg, outline=badge_border, width=1)

	code_label = f'PASS CODE: {code_str}'
	bbox_code = draw.textbbox((0, 0), code_label, font=font_code)
	draw.text(((W - (bbox_code[2] - bbox_code[0])) / 2, curr_y + 11), code_label, fill=code_color, font=font_code)
	curr_y += code_box_h + 10

	# Scan Instruction
	scan_text = 'SCAN AT ENTRY GATE FOR ADMISSION'
	bbox_scan = draw.textbbox((0, 0), scan_text, font=font_scan)
	draw.text(((W - (bbox_scan[2] - bbox_scan[0])) / 2, curr_y), scan_text, fill='#64748b', font=font_scan)
	curr_y += 24

	# Event dates and venue footer
	bbox_dates = draw.textbbox((0, 0), dates, font=font_info)
	draw.text(((W - (bbox_dates[2] - bbox_dates[0])) / 2, curr_y), dates, fill='#334155', font=font_info)
	curr_y += 26

	# Bottom footer bar
	footer_h = 32
	draw.rectangle([0, H - footer_h, W, H], fill='#f8fafc')
	draw.line([(0, H - footer_h), (W, H - footer_h)], fill='#e2e8f0', width=1)
	footer_text = 'ConnectFest 2027 • The Premier Business & Innovation Expo'
	bbox_foot = draw.textbbox((0, 0), footer_text, font=font_footer)
	draw.text(((W - (bbox_foot[2] - bbox_foot[0])) / 2, H - footer_h + 9), footer_text, fill='#94a3b8', font=font_footer)

	# Outer card border
	draw.rectangle([0, 0, W - 1, H - 1], outline='#cbd5e1', width=1)

	buf = io.BytesIO()
	img.save(buf, format='PNG', optimize=True)
	return buf.getvalue()


def download_pass_qr(code=None, pass_code=None, registration_id=None, pass_number=1, **kwargs):
	"""
	Downloads the high-resolution branded Gate Pass Card directly as a PNG attachment.
	Includes ConnectFest 2027 header, Pass Code, Pass Type (Exhibitor / Visitor / BNI Event), and QR Code.
	"""
	req_data = extract_request_data(kwargs)
	clean_code = (code or pass_code or req_data.get("code") or req_data.get("pass_code") or "").strip()
	reg_id = (registration_id or req_data.get("registration_id") or "").strip()

	if not clean_code and reg_id:
		p_num = cint(pass_number or req_data.get("pass_number") or 1)
		field = "pass_2_pass_code" if p_num == 2 else "pass_code"
		clean_code = frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, field) or ""
		if not clean_code and p_num == 1:
			clean_code = frappe.db.get_value("ConnectFest Visitor Registration", {"registration_id": reg_id}, "pass_code") or ""
		if not clean_code:
			clean_code = frappe.db.get_value("ConnectFest BNI Event Registration", {"registration_id": reg_id}, "pass_code") or ""
		if not clean_code:
			clean_code = reg_id

	if not clean_code:
		clean_code = "CONNECTFEST-GATE-PASS"

	safe_filename_code = re.sub(r"[^A-Za-z0-9_-]", "_", clean_code)
	filename = f"ConnectFest_Pass_{safe_filename_code}.png"

	# Determine pass type and details for branding the downloaded pass card
	pass_type = None
	attendee_name = ""
	attendee_subtitle = ""
	try:
		# 1. Visitor Registration Check
		vis = frappe.db.get_value(
			"ConnectFest Visitor Registration",
			{"pass_code": clean_code},
			["full_name", "which_day"],
			as_dict=True,
		)
		if not vis and reg_id:
			vis = frappe.db.get_value(
				"ConnectFest Visitor Registration",
				{"registration_id": reg_id},
				["full_name", "which_day"],
				as_dict=True,
			)
		if vis:
			pass_type = "VISITOR PASS"
			attendee_name = vis.full_name or "Visitor"
			which = vis.which_day or "Both days"
			attendee_subtitle = f"Access: {which}"
		else:
			# 2. Exhibitor Registration Check (correct field: stall_number)
			ex = frappe.db.get_value(
				"ConnectFest Registration",
				{"pass_code": clean_code},
				["full_name", "company_name", "stall_number", "stall_category"],
				as_dict=True,
			)
			if not ex:
				ex = frappe.db.get_value(
					"ConnectFest Registration",
					{"pass_2_pass_code": clean_code},
					["full_name", "company_name", "stall_number", "stall_category"],
					as_dict=True,
				)
			if not ex and reg_id:
				ex = frappe.db.get_value(
					"ConnectFest Registration",
					{"registration_id": reg_id},
					["full_name", "company_name", "stall_number", "stall_category"],
					as_dict=True,
				)
			if ex:
				pass_type = "EXHIBITOR PASS"
				attendee_name = ex.full_name or "Exhibitor"
				stalls = ex.stall_number or "Assigned"
				cat = ex.stall_category or "Regular"
				comp = ex.company_name or ""
				attendee_subtitle = f"{comp} • Stall: {stalls} ({cat})" if comp else f"Stall: {stalls} ({cat})"
			else:
				# 3. BNI Event Registration Check
				bni = frappe.db.get_value(
					"ConnectFest BNI Event Registration",
					{"pass_code": clean_code},
					["full_name", "company_name", "chapter_name", "event_name"],
					as_dict=True,
				)
				if not bni and reg_id:
					bni = frappe.db.get_value(
						"ConnectFest BNI Event Registration",
						{"registration_id": reg_id},
						["full_name", "company_name", "chapter_name", "event_name"],
						as_dict=True,
					)
				if bni:
					pass_type = "BNI EVENT PASS"
					attendee_name = bni.full_name or "Event Attendee"
					comp = bni.company_name or ""
					chap = bni.chapter_name or "BNI Member"
					attendee_subtitle = f"{comp} • Chapter: {chap}" if comp else f"Chapter: {chap}"
	except Exception:
		pass

	if not pass_type:
		if "VIS" in clean_code.upper():
			pass_type = "VISITOR PASS"
			attendee_subtitle = "Visitor Access"
		elif "BNI" in clean_code.upper() or "EVT" in clean_code.upper():
			pass_type = "BNI EVENT PASS"
			attendee_subtitle = "BNI Member Access"
		else:
			pass_type = "EXHIBITOR PASS"
			attendee_subtitle = "Exhibitor Gate Pass"

	event_dates_venue = "20 & 21 Feb 2027 • Rajapalayam"
	try:
		settings = frappe.get_single("ConnectFest Settings")
		ed = settings.get("event_dates") or "20 & 21 Feb 2027"
		ev = settings.get("event_venue") or "Rajapalayam"
		event_dates_venue = f"{ed} • {ev}"
	except Exception:
		pass

	# Cache check
	cache_key = f"pass_card_png_{safe_filename_code}"
	png_bytes = None
	try:
		png_bytes = frappe.cache().get_value(cache_key)
	except Exception:
		pass

	if not png_bytes:
		try:
			png_bytes = generate_pass_card_png_bytes(
				clean_code,
				pass_type=pass_type,
				name=attendee_name,
				subtitle=attendee_subtitle,
				dates=event_dates_venue,
			)
			if png_bytes:
				try:
					frappe.cache().set_value(cache_key, png_bytes, expires_in_sec=86400)
				except Exception:
					pass
		except Exception:
			pass

	if not png_bytes:
		try:
			png_bytes = generate_local_qr_png_bytes(clean_code, scale=12, border=2)
		except Exception:
			pass

	if not png_bytes:
		# Fallback to local pyqrcode SVG
		try:
			import pyqrcode
			qr = pyqrcode.create(str(clean_code))
			buf = io.BytesIO()
			qr.svg(buf, scale=10)
			svg_bytes = buf.getvalue()
			frappe.local.response.filename = f"ConnectFest_Pass_QR_{safe_filename_code}.svg"
			frappe.local.response.filecontent = svg_bytes
			frappe.local.response.type = "download"
			frappe.local.response.content_type = "image/svg+xml"
			return
		except Exception as e:
			frappe.throw(f"Unable to generate QR code: {str(e)}")

	frappe.local.response.filename = filename
	frappe.local.response.filecontent = png_bytes
	frappe.local.response.type = "download"
	frappe.local.response.content_type = "image/png"
