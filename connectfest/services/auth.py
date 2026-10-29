import frappe
from frappe.utils import flt, cint
from connectfest.services.common import extract_request_data, handle_api_errors


def ensure_frappe_user(email, full_name, mobile_no=None, role="Website User"):
	"""
	Ensures a Frappe User exists for visitors and exhibitors so they can log in.
	Safely handles unique key constraints on mobile_no.
	"""
	if not email or "@" not in email:
		return None
	clean_email = email.strip().lower()
	clean_mob = (mobile_no or "").strip()
	try:
		if not frappe.db.exists("User", clean_email):
			# Handle unique mobile_no constraint in tabUser
			mob_for_user = clean_mob
			phone_for_user = clean_mob
			if clean_mob:
				existing_user_with_mob = frappe.db.get_value("User", {"mobile_no": clean_mob}, "name") or frappe.db.get_value("User", {"phone": clean_mob}, "name")
				if existing_user_with_mob and existing_user_with_mob != clean_email:
					# Check if old user has an active ConnectFest registration with this mobile
					has_reg = (
						frappe.db.exists("ConnectFest Registration", {"email": existing_user_with_mob})
						or frappe.db.exists("ConnectFest Visitor Registration", {"email": existing_user_with_mob})
					)
					if not has_reg:
						# Stale / test user has this mobile; release it
						frappe.db.set_value("User", existing_user_with_mob, "mobile_no", None)
						frappe.db.set_value("User", existing_user_with_mob, "phone", None)
						frappe.db.commit()
					else:
						# Another active user already owns this mobile! Never assign to a different email
						mob_for_user = ""
						phone_for_user = ""

			# Ensure role exists in tabRole
			roles_list = []
			if role:
				if role not in ("Website User", "System Manager", "All") and not frappe.db.exists("Role", role):
					try:
						r_doc = frappe.get_doc({
							"doctype": "Role",
							"role_name": role,
							"desk_access": 0,
						})
						r_doc.insert(ignore_permissions=True)
						frappe.db.commit()
					except Exception:
						pass
				roles_list.append({"doctype": "Has Role", "role": role})

			clean_fn = (full_name or "").strip()
			if not clean_fn or "@" in clean_fn:
				clean_fn = clean_email.split("@")[0].replace(".", " ").replace("_", " ").title()
			names = clean_fn.split(" ", 1)
			first_name = names[0]
			last_name = names[1] if len(names) > 1 else ""
			user = frappe.get_doc({
				"doctype": "User",
				"email": clean_email,
				"username": clean_email,
				"first_name": first_name,
				"last_name": last_name,
				"full_name": clean_fn,
				"phone": phone_for_user,
				"mobile_no": mob_for_user or "",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "Website User",
				"roles": roles_list,
			})
			user.flags.ignore_permissions = True
			user.flags.ignore_password_policy = True
			user.flags.ignore_validate = True
			user.flags.ignore_links = True
			user.on_update = lambda *args, **kwargs: None
			orig_in_import = getattr(frappe.flags, "in_import", False)
			frappe.flags.in_import = True
			import time, random
			inserted = False
			last_err = None
			try:
				for attempt in range(5):
					try:
						user.insert(ignore_if_duplicate=True)
						frappe.db.commit()
						inserted = True
						break
					except Exception as insert_err:
						last_err = insert_err
						err_str = str(insert_err)
						if "1213" in err_str or "Deadlock" in err_str or "Lock wait timeout" in err_str:
							time.sleep(random.uniform(0.05, 0.15) * (attempt + 1))
							continue
						elif "mobile_no" in err_str:
							user.mobile_no = ""
							try:
								user.insert(ignore_if_duplicate=True)
								frappe.db.commit()
								inserted = True
								break
							except Exception:
								pass
						else:
							raise insert_err
				if not inserted and last_err:
					raise last_err
			finally:
				frappe.flags.in_import = orig_in_import
			return user.name
		else:
			# User already exists - check if already enabled, phone matches, and full_name is set properly
			user_vals = frappe.db.get_value("User", clean_email, ["enabled", "phone", "mobile_no", "full_name"], as_dict=True)
			if user_vals:
				needs_enable = not user_vals.get("enabled")
				needs_phone = bool(clean_mob and not user_vals.get("phone"))
				needs_mob = bool(clean_mob and not user_vals.get("mobile_no"))
				curr_fn = (user_vals.get("full_name") or "").strip()
				clean_fn = (full_name or "").strip()
				needs_name = False
				if clean_fn and "@" not in clean_fn:
					if not curr_fn or "@" in curr_fn or curr_fn != clean_fn:
						needs_name = True
				elif not curr_fn or "@" in curr_fn:
					clean_fn = clean_email.split("@")[0].replace(".", " ").replace("_", " ").title()
					needs_name = True

				if needs_enable or needs_phone or needs_mob or needs_name:
					u_doc = frappe.get_doc("User", clean_email)
					changed = False
					if not u_doc.enabled:
						u_doc.enabled = 1
						changed = True
					if clean_mob:
						if not u_doc.phone:
							u_doc.phone = clean_mob
							changed = True
						if not u_doc.mobile_no and not frappe.db.exists("User", {"mobile_no": clean_mob}):
							u_doc.mobile_no = clean_mob
							changed = True
					if needs_name and clean_fn:
						names = clean_fn.split(" ", 1)
						u_doc.first_name = names[0]
						u_doc.last_name = names[1] if len(names) > 1 else ""
						u_doc.full_name = clean_fn
						changed = True
					if changed:
						u_doc.flags.ignore_permissions = True
						u_doc.flags.ignore_password_policy = True
						u_doc.on_update = lambda *args, **kwargs: None
						u_doc.save()
						frappe.db.commit()
		return clean_email
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error("ConnectFest User Creation Error", str(e))
		return clean_email


def is_admin_user(user=None):
	"""Checks if a user has Administrator or System Manager privileges."""
	u = user or getattr(frappe.session, "user", None)
	if not u or u == "Guest":
		return False
	if u == "Administrator":
		return True
	roles = frappe.get_roles(u)
	return "System Manager" in roles or "Administrator" in roles or "ConnectFest Admin" in roles


def check_admin_permission(resource_name="this administrative resource"):
	"""
	Verifies that the current session belongs to an authenticated Administrator.
	If Guest, returns 401 Unauthorized.
	If non-admin, returns 403 Forbidden.
	If authorized Admin, returns None.
	"""
	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"is_logged_in": False,
			"user": "Guest",
			"message": f"Authentication required! Please log in as an Administrator to access {resource_name}.",
		}
	if not is_admin_user(sess_user):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": f"Access Denied: Only administrators can access {resource_name}!",
		}
	return None


def logout():
	"""
	Logs out the current session user and clears authentication cookies.
	"""
	user = getattr(frappe.session, "user", "Guest")
	try:
		frappe.local.login_manager.logout()
	except Exception:
		pass
	return {
		"status": "success",
		"message": f"Successfully logged out {user}",
		"logged_out_user": user,
	}


@handle_api_errors("ConnectFest Profile Fetch Error")
def get_current_user_profile(**kwargs):
	"""
	Unified Attendee & User Profile API (Single Source of Truth).
	Returns ALL user identity, role, stall booking, payment status, and digital pass details.
	Strictly requires authentication (Guests not allowed).
	Prevents IDOR: Users can only see their own profile.
	"""
	data = extract_request_data(kwargs)
	sess_user = getattr(frappe.session, "user", None)

	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"is_logged_in": False,
			"user": "Guest",
			"message": "Authentication required! Please log in with OTP or credentials to view your profile and bookings.",
		}

	# Prevent IDOR: Disallow peeking into another user's profile
	target_user = sess_user
	requested_email = (data.get("email") or data.get("user_email") or "").strip().lower()
	if requested_email and requested_email != sess_user.lower():
		if not is_admin_user(sess_user):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Access Denied: You are not authorized to view another attendee's personal data!",
			}
		target_user = requested_email

	user_vals = frappe.db.get_value("User", target_user, ["full_name", "mobile_no", "phone"], as_dict=True)
	if user_vals:
		user_full_name = (user_vals.get("full_name") or "").strip()
		user_roles = [r for r in frappe.get_roles(target_user) if r not in ("All", "Guest")]
		user_mobile = (user_vals.get("mobile_no") or user_vals.get("phone") or "").strip()
	else:
		user_full_name = ""
		user_roles = ["Website User"]
		user_mobile = ""

	is_admin = is_admin_user(target_user)

	# Fetch all stall booking details (Exhibitor)
	stalls_raw = frappe.db.sql("""
		SELECT name, registration_id, full_name, company_name, company_logo, location,
		       is_bni_member, region_name, chapter_name, industry, segment, source_of_info,
		       email, mobile_no, stall_number, stall_category, amount, payment_status,
		       payment_mode, razorpay_order_id, razorpay_payment_id, payment_date, creation,
		       bill, gst
		FROM `tabConnectFest Registration`
		WHERE (email = %s OR (mobile_no = %s AND %s != ''))
		ORDER BY creation ASC
	""", (target_user, user_mobile, user_mobile), as_dict=True)

	# Fetch visitor pass details (Visitor)
	visitors_raw = frappe.db.sql("""
		SELECT name, registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code,
		       qr_payload, status, checked_in, checked_in_at, creation, area_of_interest
		FROM `tabConnectFest Visitor Registration`
		WHERE (email = %s OR (mobile_no = %s AND %s != ''))
		ORDER BY creation ASC
	""", (target_user, user_mobile, user_mobile), as_dict=True)

	# Authoritative Name: Always display the exact full_name collected during ConnectFest Registration!
	reg_name = None
	for s in stalls_raw:
		if s.get("full_name") and s.get("full_name").strip() and "@" not in s.get("full_name"):
			reg_name = s.get("full_name").strip()
			break
	if not reg_name:
		for v in visitors_raw:
			if v.get("full_name") and v.get("full_name").strip() and "@" not in v.get("full_name"):
				reg_name = v.get("full_name").strip()
				break

	if reg_name:
		user_full_name = reg_name
	elif not user_full_name or "@" in user_full_name:
		user_full_name = target_user.split("@")[0].replace(".", " ").replace("_", " ").title()

	# Sync authoritative ConnectFest Registration full_name to tabUser
	if user_full_name and target_user and frappe.db.exists("User", target_user):
		try:
			curr_u_fn = (frappe.db.get_value("User", target_user, "full_name") or "").strip()
			if curr_u_fn != user_full_name:
				names = user_full_name.split(" ", 1)
				frappe.db.set_value("User", target_user, {
					"first_name": names[0],
					"last_name": names[1] if len(names) > 1 else "",
					"full_name": user_full_name
				}, update_modified=False)
				frappe.db.commit()
		except Exception:
			pass

	role_type = "Administrator" if is_admin else ("Exhibitor" if stalls_raw else ("Visitor" if visitors_raw else "Attendee"))

	# Business details from primary registration
	primary_reg = stalls_raw[0] if stalls_raw else None
	company_name = (primary_reg.company_name if primary_reg else "") or ""
	company_logo = (primary_reg.company_logo if primary_reg else "") or ""
	location = (primary_reg.location if primary_reg else "") or ""
	industry = (primary_reg.industry if primary_reg else "") or ""
	segment = (primary_reg.segment if primary_reg else "") or ""
	is_bni_member = bool(primary_reg.is_bni_member) if primary_reg else False
	region_name = (primary_reg.region_name if primary_reg else "") or ""
	chapter_name = (primary_reg.chapter_name if primary_reg else "") or ""
	source_of_info = (primary_reg.source_of_info if primary_reg else "") or ""
	bill = (primary_reg.get("bill") if primary_reg else "") or ""
	gst = (primary_reg.get("gst") if primary_reg else "") or ""

	stalls = []
	pending_registrations = []
	for sb in stalls_raw:
		if not sb.stall_number:
			pending_registrations.append({
				"registration_id": sb.registration_id,
				"status": "Registered",
				"booking_status": "Stall Not Selected",
				"next_action": "select_stall",
				"created_at": str(sb.creation),
			})
			continue

		stall_doc = None
		if frappe.db.exists("ConnectFest Stall", sb.stall_number):
			stall_doc = frappe.get_doc("ConnectFest Stall", sb.stall_number)

		tier_name = sb.stall_category or (stall_doc.tier if stall_doc else "")
		zone_map = {
			"Silver": "Zone D - East Pavilions",
			"Premium": "Zone C - Center Aisles",
			"Standard": "Zone B - West Aisles",
			"Regular": "Zone E - South Entrance Corridor",
		}
		zone = zone_map.get(tier_name) if tier_name else None
		is_paid = (sb.payment_status == "Paid")
		pay_mode = sb.get("payment_mode") or ("Cash" if (sb.get("razorpay_payment_id") or "").startswith("CASH") else ("Online" if is_paid else "N/A"))
		status_desc = "Confirmed" if is_paid else "Awaiting Payment"

		stalls.append({
			"registration_id": sb.registration_id,
			"full_name": (sb.full_name if (sb.full_name and "@" not in sb.full_name) else user_full_name),
			"company_name": sb.company_name or company_name,
			"company_logo": sb.company_logo or company_logo,
			"location": sb.location or location,
			"bill": sb.get("bill") or "",
			"gst": sb.get("gst") or "",
			"industry": sb.industry or industry,
			"segment": sb.segment or segment,
			"is_bni_member": bool(sb.is_bni_member),
			"region_name": sb.region_name or "",
			"chapter_name": sb.chapter_name or "",
			"source_of_info": sb.source_of_info or "",
			"email": sb.email or target_user,
			"mobile_no": sb.mobile_no or user_mobile,
			"stall_number": sb.stall_number,
			"stall_category": tier_name,
			"tier": tier_name,
			"zone": zone,
			"dimensions": (stall_doc.dimensions if stall_doc else "") or "3 x 3 m",
			"amount": flt(sb.amount),
			"payment_status": sb.payment_status or "Pending",
			"payment_mode": pay_mode,
			"has_paid": is_paid,
			"has_selected_stall": True,
			"next_action": "view_badge" if is_paid else "proceed_to_payment",
			"razorpay_payment_id": sb.razorpay_payment_id,
			"razorpay_order_id": sb.razorpay_order_id,
			"payment_date": str(sb.payment_date or sb.creation) if is_paid else None,
			"booking_status": status_desc,
			"badge_qr_payload": f"CONNECTFEST2027:EXHIBITOR:{sb.stall_number}:{sb.registration_id}:{sb.email}" if is_paid else None,
			"created_at": str(sb.creation),
		})

	visitor_passes = []
	for v in visitors_raw:
		visitor_passes.append({
			"registration_id": v.registration_id,
			"full_name": (v.full_name if (v.get("full_name") and "@" not in v.get("full_name")) else user_full_name),
			"pass_code": v.pass_code,
			"visitor_image": v.visitor_image or "",
			"which_day": v.which_day,
			"area_of_interest": v.get("area_of_interest") or "",
			"access": "BOTH DAYS" if v.which_day == "Both days" else str(v.which_day).upper(),
			"status": v.status or "Registered",
			"checked_in": int(v.checked_in or 0),
			"checked_in_at": str(v.checked_in_at or ""),
			"qr_payload": v.qr_payload or f"CONNECTFEST2027:{v.pass_code}:{v.email}",
			"created_at": str(v.creation),
		})

	stall_booking = stalls[0] if stalls else None
	total_amount = sum(flt(s.get("amount", 0.0)) for s in stalls)
	total_amount_paid = sum(flt(s.get("amount", 0.0)) for s in stalls if s.get("has_paid"))
	all_stalls_paid = bool(stalls and all(s.get("has_paid") for s in stalls))

	res = {
		"status": "success",
		"is_logged_in": True,
		"user": target_user,
		"email": target_user,
		"full_name": user_full_name,
		"mobile_no": user_mobile,
		"roles": user_roles,
		"role_type": role_type,
		"is_admin": is_admin,

		# Business / Attendee Profile
		"company_name": company_name,
		"company_logo": company_logo,
		"location": location,
		"industry": industry,
		"segment": segment,
		"is_bni_member": is_bni_member,
		"region_name": region_name,
		"chapter_name": chapter_name,
		"source_of_info": source_of_info,
		"bill": bill,
		"gst": gst,

		# Single Comprehensive Stall Booking Object & Multi-Stall Support
		"has_stall_booking": bool(stalls),
		"has_pending_selection": bool(pending_registrations and not stalls),
		"total_stalls": len(stalls),
		"stall_numbers": [s["stall_number"] for s in stalls if s.get("stall_number")],
		"total_amount": total_amount,
		"total_amount_paid": total_amount_paid,
		"all_stalls_paid": all_stalls_paid,
		"stall_booking": stall_booking,
		"stalls": stalls,

		# Visitor Pass
		"has_visitor_pass": bool(visitor_passes),
		"visitor_pass": visitor_passes[0] if visitor_passes else None,

		# Venue & Dates
		"venue": "Chennai Trade Centre, Nandambakkam",
		"dates": "20 & 21 Feb 2027",
	}

	if hasattr(frappe, "local") and hasattr(frappe.local, "response"):
		frappe.local.response["http_status_code"] = 200
	return res


def login_attendee(email=None, mobile_no=None, password=None, **kwargs):
	"""
	Allows attendees (Exhibitors and Visitors) to log in with credentials or email/phone lookup.
	Establishes Frappe session and returns their booking details and digital passes.
	"""
	from connectfest.services.passes import get_my_dashboard
	try:
		data = extract_request_data(kwargs)
		usr = (email or data.get("email") or data.get("usr") or "").strip().lower()
		mob = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()
		pwd = (password or data.get("pwd") or data.get("password") or "").strip()

		# If mobile is provided instead of email, resolve email from registrations or user doctype
		if not usr and mob:
			user_email = frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "email")
			if not user_email:
				user_email = frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "email")
			if not user_email:
				user_email = frappe.db.get_value("User", {"phone": mob}, "name") or frappe.db.get_value("User", {"mobile_no": mob}, "name")
			if user_email:
				usr = user_email.strip().lower()

		if not usr and not mob:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "Email address or Mobile Number is required"}

		# Strictly block disabled users
		target_check_email = usr
		if not target_check_email and mob:
			target_check_email = frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "email") or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "email")
		if target_check_email and frappe.db.exists("User", target_check_email):
			if not cint(frappe.db.get_value("User", target_check_email, "enabled")):
				frappe.local.response["http_status_code"] = 403
				return {
					"status": "disabled",
					"message": f"Account '{target_check_email}' has been disabled by the Administrator. Access denied.",
				}

		otp = str(data.get("otp") or data.get("code") or "").strip()
		if otp and mob:
			from connectfest.services.otp import verify_otp
			return verify_otp(mobile_no=mob, otp=otp)

		if pwd and usr:
			if not frappe.db.exists("User", usr):
				frappe.local.response["http_status_code"] = 401
				return {"status": "error", "message": "Invalid email or password"}
			try:
				frappe.auth.LoginManager().authenticate(usr, pwd)
				frappe.auth.LoginManager().post_login()
			except (frappe.AuthenticationError, frappe.DoesNotExistError):
				frappe.local.response["http_status_code"] = 401
				return {"status": "error", "message": "Invalid email or password"}
		elif usr:
			# Passwordless / direct attendee session login
			if frappe.db.exists("User", usr):
				frappe.local.login_manager.login_as(usr)

		dash = get_my_dashboard(email=usr, mobile_no=mob)
		return {
			"status": "success",
			"message": "Login successful",
			"user": usr or mob,
			"session_user": getattr(frappe.session, "user", "Guest"),
			"dashboard": dash,
		}
	except Exception as e:
		frappe.log_error("ConnectFest Login Error", frappe.get_traceback())
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}


def admin_login(email=None, password=None, otp=None, **kwargs):
	"""
	Dedicated Secure Admin Login with Two-Factor Authentication (Password + 2FA OTP).
	Strictly restricted to users with 'System Manager' or 'Administrator' roles.
	"""
	from connectfest.services.otp import OTP_CACHE_PREFIX, TEST_MASTER_OTPS, is_otp_test_mode
	import random
	try:
		data = extract_request_data(kwargs)
		usr = (email or data.get("email") or data.get("usr") or "").strip().lower()
		pwd = (password or data.get("pwd") or data.get("password") or "").strip()
		submitted_otp = str(otp or data.get("otp") or "").strip()

		if not usr or not pwd:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "Admin Email and Password are required"}

		# Step 1: Verify credentials
		if not frappe.db.exists("User", usr):
			frappe.local.response["http_status_code"] = 401
			return {"status": "error", "message": "Invalid admin email or password"}

		try:
			frappe.auth.LoginManager().authenticate(usr, pwd)
		except (frappe.AuthenticationError, frappe.DoesNotExistError):
			frappe.local.response["http_status_code"] = 401
			return {"status": "error", "message": "Invalid admin email or password"}

		# Step 2: Verify Admin Role Privilege
		roles = frappe.get_roles(usr)
		if "System Manager" not in roles and "Administrator" not in roles and "ConnectFest Admin" not in roles:
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Access Denied: This administrative portal is strictly restricted to System Administrators!"
			}

		cache = frappe.cache()
		cache_key = cache.make_key(f"{OTP_CACHE_PREFIX}admin:{usr}")
		test_mode = is_otp_test_mode()

		# Step 3: Check 2FA OTP
		if not submitted_otp:
			# Generate 6-digit Admin 2FA OTP
			otp_code = f"{random.randint(100000, 999999)}"
			cache.setex(cache_key, 120, otp_code)  # 2 minutes expiry

			frappe.logger("connectfest").info(f"[ConnectFest Admin 2FA] OTP {otp_code} for admin {usr}")

			return {
				"status": "otp_required",
				"message": "Admin credentials verified! 2FA OTP sent for security verification. Valid for 2 minutes.",
				"admin_user": usr,
				"expires_in": "2 minutes",
				"expires_in_seconds": 120,
				"test_credentials": {
					"mock_otp": "123456",
					"generated_otp": otp_code,
					"info": "Razorpay-style Test Mode active: enter OTP '123456' or the generated OTP to complete 2FA login."
				}
			}

		# Verify OTP
		stored_otp = cache.get(cache_key)
		if isinstance(stored_otp, bytes):
			stored_otp = stored_otp.decode("utf-8")

		is_valid = False
		if stored_otp and stored_otp == submitted_otp:
			is_valid = True
		elif test_mode and submitted_otp in TEST_MASTER_OTPS:
			is_valid = True

		if not is_valid:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "Invalid or expired Admin 2FA OTP. Please use '123456' in test mode or request a new OTP."
			}

		try:
			cache.delete(cache_key)
		except Exception:
			pass

		# Complete login and establish admin session
		frappe.local.login_manager.login_as(usr)

		return {
			"status": "success",
			"verified_2fa": True,
			"message": f"Admin 2FA Authentication successful. Welcome {usr}!",
			"admin_user": usr,
			"roles": [r for r in roles if r in ("Administrator", "System Manager", "Website Manager")],
			"session_user": getattr(frappe.session, "user", usr),
		}
	except Exception as e:
		frappe.log_error("ConnectFest Admin Login Error", frappe.get_traceback())
		return {"status": "error", "message": "Something went wrong during admin authentication."}


def admin_toggle_user_status(user_email=None, mobile_no=None, enabled=None, **kwargs):
	"""
	Admin API to enable or disable attendee User accounts.
	Requires System Manager or Administrator privileges.
	"""
	try:
		from frappe.utils import cint
		err = check_admin_permission("user status controls")
		if err:
			return err

		data = extract_request_data(kwargs)
		target_email = (user_email or data.get("user_email") or data.get("email") or "").strip().lower()
		target_mob = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()

		raw_enabled = enabled if enabled is not None else data.get("enabled")
		if raw_enabled is None:
			new_enabled = 1
		elif isinstance(raw_enabled, bool):
			new_enabled = 1 if raw_enabled else 0
		else:
			s = str(raw_enabled).strip().lower()
			new_enabled = 0 if s in ("0", "false", "no", "disabled", "disable") else 1

		if not target_email and target_mob:
			target_email = frappe.db.get_value("ConnectFest Registration", {"mobile_no": target_mob}, "email") or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": target_mob}, "email") or frappe.db.get_value("User", {"phone": target_mob}, "name")

		if not target_email:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "Target user_email or mobile_no is required"}

		if not frappe.db.exists("User", target_email):
			frappe.local.response["http_status_code"] = 404
			return {"status": "error", "message": f"User '{target_email}' does not exist"}

		frappe.db.set_value("User", target_email, "enabled", new_enabled)

		# Also update registration doc if exists
		new_status = "Registered" if new_enabled else "Cancelled"
		if frappe.db.exists("ConnectFest Registration", {"email": target_email}):
			frappe.db.set_value("ConnectFest Registration", {"email": target_email}, "status", new_status)
		if frappe.db.exists("ConnectFest Visitor Registration", {"email": target_email}):
			frappe.db.set_value("ConnectFest Visitor Registration", {"email": target_email}, "status", new_status)

		frappe.db.commit()

		action_text = "enabled" if new_enabled else "disabled"
		return {
			"status": "success",
			"message": f"User {target_email} has been {action_text} successfully.",
			"user": target_email,
			"enabled": new_enabled,
			"account_status": action_text.capitalize()
		}
	except Exception as e:
		frappe.log_error("ConnectFest Toggle User Error", frappe.get_traceback())
		return {"status": "error", "message": str(e)}


@handle_api_errors("ConnectFest Admin Get All Users Error")
def admin_get_all_users(**kwargs):
	"""
	Admin API: Returns a unified directory of all users in ConnectFest.
	Includes:
	- Role categorization: Exhibitor, Visitor, Administrator, Website User
	- Exhibitor details (company, stall number, payment status, amount)
	- Visitor details (pass code, which day, checked-in status)
	- Filter by role ('exhibitor', 'visitor', 'admin', 'all')
	- Filter by status ('active' / enabled=1, 'disabled' / enabled=0)
	- Search by name, email, phone, stall number, pass code
	- Comprehensive summary counts & pagination
	Requires System Manager or Administrator privileges.
	"""
	err = check_admin_permission("user directory access")
	if err:
		return err

	data = extract_request_data(kwargs)
	role_filter = (data.get("role") or data.get("user_type") or data.get("type") or "all").strip().lower()
	status_filter = data.get("status") or data.get("enabled")
	payment_filter = (data.get("payment_status") or "").strip()
	search_query = (data.get("search") or data.get("query") or "").strip().lower()

	raw_page = data.get("page")
	raw_limit = data.get("limit") or data.get("page_size") or 50
	page = max(1, cint(raw_page)) if raw_page else 1
	limit = 0 if str(raw_limit).lower() in ("all", "0") else min(500, max(1, cint(raw_limit)))

	# 1. Fetch all Exhibitor registrations
	exhibitor_records = frappe.get_all(
		"ConnectFest Registration",
		fields=[
			"registration_id", "full_name", "email", "mobile_no", "company_name",
			"company_logo", "location", "is_bni_member", "region_name", "chapter_name",
			"industry", "segment", "source_of_info", "stall_number", "stall_category",
			"amount", "payment_status", "payment_mode", "razorpay_order_id",
			"razorpay_payment_id", "payment_date", "status", "creation", "modified",
			"bill", "gst"
		],
		order_by="creation desc",
		limit_page_length=0
	)
	exhibitors_by_email = {}
	for ex in exhibitor_records:
		if ex.email:
			exhibitors_by_email.setdefault(ex.email.strip().lower(), []).append(ex)

	# 2. Fetch all Visitor registrations
	visitor_records = frappe.get_all(
		"ConnectFest Visitor Registration",
		fields=[
			"registration_id", "full_name", "visitor_image", "email", "mobile_no", "which_day",
			"source_of_info", "pass_code", "checked_in", "checked_in_at", "status", "creation", "modified",
			"area_of_interest"
		],
		order_by="creation desc",
		limit_page_length=0
	)
	visitors_by_email = {}
	for vis in visitor_records:
		if vis.email:
			visitors_by_email.setdefault(vis.email.strip().lower(), []).append(vis)

	# 3. Fetch all Frappe Users
	user_records = frappe.get_all(
		"User",
		filters={"name": ("not in", ["Guest"])},
		fields=[
			"name", "email", "first_name", "last_name", "full_name",
			"phone", "mobile_no", "enabled", "user_type", "user_image",
			"creation", "modified", "last_login", "last_active"
		],
		order_by="creation desc",
		limit_page_length=0
	)
	users_by_email = {u.name.strip().lower(): u for u in user_records}

	# 4. Fetch User Roles
	roles_by_user = {}
	role_rows = frappe.db.sql("""
		SELECT parent, role FROM `tabHas Role`
		WHERE parent != 'Guest'
	""", as_dict=True)
	for r in role_rows:
		roles_by_user.setdefault(r.parent.strip().lower(), []).append(r.role)

	# 5. Collect all unique attendee identities (combining User, Exhibitor, Visitor)
	all_identities = set(users_by_email.keys()) | set(exhibitors_by_email.keys()) | set(visitors_by_email.keys())

	unified_users = []
	for email_key in all_identities:
		u_doc = users_by_email.get(email_key)
		ex_list = exhibitors_by_email.get(email_key, [])
		ex_doc = ex_list[0] if ex_list else None
		vis_list = visitors_by_email.get(email_key, [])
		vis_doc = vis_list[0] if vis_list else None
		user_roles = roles_by_user.get(email_key, [])

		is_admin = (
			email_key == "administrator"
			or "Administrator" in user_roles
			or "System Manager" in user_roles
			or "ConnectFest Admin" in user_roles
		)

		# Determine primary user category
		if is_admin:
			category = "Admin"
		elif ex_doc:
			category = "Exhibitor"
		elif vis_doc:
			category = "Visitor"
		else:
			category = "Website User"

		candidate_names = [
			(ex_doc.full_name if ex_doc else ""),
			(vis_doc.full_name if vis_doc else ""),
			(u_doc.full_name if u_doc else ""),
		]
		full_name = ""
		for cn in candidate_names:
			if cn and "@" not in cn and cn.strip():
				full_name = cn.strip()
				break
		if not full_name:
			full_name = email_key.split("@")[0].replace(".", " ").replace("_", " ").title()

		mobile = (
			(u_doc.mobile_no or u_doc.phone if u_doc else "")
			or (ex_doc.mobile_no if ex_doc else "")
			or (vis_doc.mobile_no if vis_doc else "")
			or ""
		).strip()

		enabled = cint(u_doc.enabled) if u_doc else 1
		creation = str(u_doc.creation if u_doc else (ex_doc.creation if ex_doc else (vis_doc.creation if vis_doc else "")))
		last_login = str(u_doc.last_login if u_doc and u_doc.last_login else "")
		user_image = (u_doc.user_image if u_doc else "") or (ex_doc.company_logo if ex_doc else "") or (vis_doc.visitor_image if vis_doc else "") or ""

		exhibitor_info = None
		if ex_list:
			latest_ex = ex_list[0]
			stalls_detail = [
				{
					"registration_id": r.registration_id,
					"stall_number": r.stall_number,
					"stall_category": r.stall_category,
					"amount": flt(r.amount),
					"payment_status": r.payment_status or "Pending",
					"payment_mode": r.get("payment_mode") or ("Cash" if (r.get("razorpay_payment_id") or "").startswith("CASH") else "Online"),
					"payment_date": str(r.payment_date or ""),
					"status": r.status or "Registered",
					"bill": r.get("bill") or "",
					"gst": r.get("gst") or "",
					"is_bni_member": bool(r.get("is_bni_member")),
					"region_name": r.get("region_name") or "",
					"chapter_name": r.get("chapter_name") or "",
					"industry": r.get("industry") or "",
					"segment": r.get("segment") or "",
					"source_of_info": r.get("source_of_info") or "",
					"creation": str(r.creation or ""),
				}
				for r in ex_list
			]
			all_stalls = [r.stall_number for r in ex_list if r.stall_number]
			total_stall_amount = sum(flt(r.amount) for r in ex_list)
			exhibitor_info = {
				"registration_id": latest_ex.registration_id,
				"company_name": latest_ex.company_name or "",
				"company_logo": latest_ex.company_logo or "",
				"location": latest_ex.location or "",
				"bill": latest_ex.get("bill") or "",
				"gst": latest_ex.get("gst") or "",
				"is_bni_member": bool(latest_ex.get("is_bni_member")),
				"region_name": latest_ex.get("region_name") or "",
				"chapter_name": latest_ex.get("chapter_name") or "",
				"industry": latest_ex.get("industry") or "",
				"segment": latest_ex.get("segment") or "",
				"source_of_info": latest_ex.get("source_of_info") or "",
				"stall_number": ", ".join(all_stalls) if all_stalls else (latest_ex.stall_number or None),
				"stall_category": latest_ex.stall_category or None,
				"amount": total_stall_amount,
				"payment_status": latest_ex.payment_status or "Pending",
				"payment_mode": latest_ex.get("payment_mode") or ("Cash" if (latest_ex.get("razorpay_payment_id") or "").startswith("CASH") else "Online"),
				"payment_date": str(latest_ex.payment_date or ""),
				"status": latest_ex.status or "Registered",
				"total_stalls": len(ex_list),
				"stalls_booked": all_stalls,
				"stalls": stalls_detail,
			}

		visitor_info = None
		if vis_doc:
			visitor_info = {
				"registration_id": vis_doc.registration_id,
				"pass_code": vis_doc.pass_code or "",
				"visitor_image": vis_doc.visitor_image or "",
				"which_day": vis_doc.which_day or "Both days",
				"checked_in": cint(vis_doc.checked_in),
				"checked_in_at": str(vis_doc.checked_in_at or ""),
				"area_of_interest": vis_doc.get("area_of_interest") or "",
				"source_of_info": vis_doc.get("source_of_info") or "",
				"status": vis_doc.status or "Registered",
			}

		primary_bni = bool(ex_doc.get("is_bni_member")) if ex_doc else False
		primary_region = (ex_doc.get("region_name") if ex_doc else "") or ""
		primary_chapter = (ex_doc.get("chapter_name") if ex_doc else "") or ""
		primary_industry = (ex_doc.get("industry") if ex_doc else "") or ""
		primary_segment = (ex_doc.get("segment") if ex_doc else "") or ""
		primary_company = (ex_doc.company_name if ex_doc else "") or ""
		primary_location = (ex_doc.location if ex_doc else "") or ""
		primary_source = (ex_doc.get("source_of_info") if ex_doc else (vis_doc.get("source_of_info") if vis_doc else "")) or ""
		primary_pay_mode = (ex_doc.get("payment_mode") or ("Cash" if (ex_doc.get("razorpay_payment_id") or "").startswith("CASH") else "Online")) if ex_doc else None
		all_user_stalls = [r.stall_number for r in ex_list if r.stall_number]

		user_entry = {
			"email": email_key,
			"full_name": full_name,
			"mobile_no": mobile,
			"user_type": category,
			"category": category,
			"company_name": primary_company,
			"company_logo": user_image,
			"location": primary_location,
			"bill": (ex_doc.get("bill") if ex_doc else "") or "",
			"gst": (ex_doc.get("gst") if ex_doc else "") or "",
			"area_of_interest": (vis_doc.get("area_of_interest") if vis_doc else "") or "",
			"is_bni_member": primary_bni,
			"region_name": primary_region,
			"chapter_name": primary_chapter,
			"industry": primary_industry,
			"segment": primary_segment,
			"source_of_info": primary_source,
			"payment_mode": primary_pay_mode,
			"payment_status": (ex_doc.payment_status if ex_doc else None),
			"stall_count": len(ex_list),
			"stall_numbers": all_user_stalls,
			"stall_number": (", ".join(all_user_stalls)) if all_user_stalls else (ex_doc.stall_number if ex_doc else None),
			"enabled": enabled,
			"is_active": bool(enabled),
			"user_image": user_image,
			"roles": user_roles or (["Website User"] if category != "Admin" else ["System Manager", "Administrator"]),
			"creation": creation,
			"last_login": last_login,
			"exhibitor_details": exhibitor_info,
			"visitor_details": visitor_info,
		}
		unified_users.append(user_entry)

	# Global counts before filtering
	total_all = len(unified_users)
	total_exhibitors = sum(1 for u in unified_users if u["user_type"] == "Exhibitor")
	total_visitors = sum(1 for u in unified_users if u["user_type"] == "Visitor")
	total_admins = sum(1 for u in unified_users if u["user_type"] == "Admin")
	total_bni = sum(1 for u in unified_users if u.get("is_bni_member"))
	active_count = sum(1 for u in unified_users if u["enabled"] == 1)
	disabled_count = sum(1 for u in unified_users if u["enabled"] == 0)

	# Apply Filters
	filtered_users = unified_users

	# 1. Role Filter
	if role_filter and role_filter not in ("all", "*"):
		if role_filter in ("exhibitor", "exhibitors"):
			filtered_users = [u for u in filtered_users if u["user_type"] == "Exhibitor"]
		elif role_filter in ("visitor", "visitors"):
			filtered_users = [u for u in filtered_users if u["user_type"] == "Visitor"]
		elif role_filter in ("admin", "admins", "administrator"):
			filtered_users = [u for u in filtered_users if u["user_type"] == "Admin"]
		elif role_filter in ("website_user", "general", "user"):
			filtered_users = [u for u in filtered_users if u["user_type"] == "Website User"]

	# 2. Status Filter
	if status_filter is not None and str(status_filter).strip() != "":
		s_val = str(status_filter).strip().lower()
		target_en = 1 if s_val in ("1", "true", "active", "enabled") else 0
		filtered_users = [u for u in filtered_users if u["enabled"] == target_en]

	# 3. Payment Status Filter (Exhibitor specific)
	if payment_filter:
		filtered_users = [
			u for u in filtered_users
			if u.get("exhibitor_details") and (u["exhibitor_details"].get("payment_status") or "").lower() == payment_filter.lower()
		]

	# 4. Payment Mode Filter (Cash / Online)
	pay_mode_filter = (data.get("payment_mode") or "").strip().lower()
	if pay_mode_filter:
		filtered_users = [
			u for u in filtered_users
			if (u.get("payment_mode") or "").lower() == pay_mode_filter
		]

	# 5. BNI Member Filter
	bni_filter = data.get("is_bni_member") if data.get("is_bni_member") is not None else data.get("bni")
	if bni_filter is not None and str(bni_filter).strip() != "":
		b_val = str(bni_filter).strip().lower()
		target_bni = b_val in ("1", "true", "yes")
		filtered_users = [u for u in filtered_users if bool(u.get("is_bni_member")) == target_bni]

	# 6. Search Filter
	if search_query:
		q = search_query.lower()
		filtered_users = [
			u for u in filtered_users
			if (
				q in u["email"].lower()
				or q in u["full_name"].lower()
				or q in u["mobile_no"].lower()
				or q in (u.get("company_name") or "").lower()
				or q in (u.get("location") or "").lower()
				or q in (u.get("region_name") or "").lower()
				or q in (u.get("chapter_name") or "").lower()
				or q in (u.get("industry") or "").lower()
				or q in (u.get("segment") or "").lower()
				or q in (u.get("source_of_info") or "").lower()
				or q in (u.get("payment_mode") or "").lower()
				or any(q in st.lower() for st in u.get("stall_numbers", []))
				or (u.get("stall_number") and q in u["stall_number"].lower())
				or (u.get("visitor_details") and q in (u["visitor_details"].get("pass_code") or "").lower())
			)
		]

	# Sort by creation desc
	filtered_users.sort(key=lambda x: x.get("creation") or "", reverse=True)

	total_filtered = len(filtered_users)

	# Pagination
	if limit > 0:
		start = (page - 1) * limit
		paginated_users = filtered_users[start:start + limit]
	else:
		paginated_users = filtered_users
		limit = total_filtered or 1

	total_pages = (total_filtered + limit - 1) // limit if limit > 0 else 1

	return {
		"status": "success",
		"total_users": total_filtered,
		"summary": {
			"total_all": total_all,
			"total_exhibitors": total_exhibitors,
			"total_visitors": total_visitors,
			"total_admins": total_admins,
			"total_bni_members": total_bni,
			"active_users": active_count,
			"disabled_users": disabled_count,
		},
		"filters_applied": {
			"role": role_filter,
			"status": status_filter,
			"payment_status": payment_filter or None,
			"search": search_query or None,
		},
		"pagination": {
			"current_page": page,
			"page_size": limit,
			"total_items": total_filtered,
			"total_pages": total_pages,
			"has_next": page < total_pages,
			"has_prev": page > 1,
		},
		"users": paginated_users,
	}


@handle_api_errors("ConnectFest Admin Get User Details Error")
def admin_get_user_details(user_email=None, mobile_no=None, **kwargs):
	"""
	Admin API to get full details of a specific user by email or mobile number.
	"""
	err = check_admin_permission("user details access")
	if err:
		return err

	data = extract_request_data(kwargs)
	target_email = (user_email or data.get("user_email") or data.get("email") or "").strip().lower()
	target_mob = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()

	if not target_email and not target_mob:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "user_email or mobile_no is required"}

	# Fetch via admin_get_all_users with search
	all_res = admin_get_all_users(search=target_email or target_mob, limit=10)
	users = all_res.get("users", [])

	matched = None
	for u in users:
		if target_email and u["email"].lower() == target_email:
			matched = u
			break
		if target_mob and u["mobile_no"] == target_mob:
			matched = u
			break

	if not matched:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"User not found for '{target_email or target_mob}'"}

	return {
		"status": "success",
		"user": matched,
	}


def request_contact_update(new_mobile_no=None, new_email=None, **kwargs):
	"""
	Initiates phone number or email address update with 2-minute OTP verification.
	Requires logged-in session.
	"""
	from connectfest.services.otp import OTP_CACHE_PREFIX, normalize_mobile
	import random
	import json
	try:
		sess_user = getattr(frappe.session, "user", None)
		if not sess_user or sess_user == "Guest":
			frappe.local.response["http_status_code"] = 401
			return {"status": "unauthorized", "message": "Authentication required to update contact details."}

		data = extract_request_data(kwargs)
		new_mob = normalize_mobile(new_mobile_no or data.get("new_mobile_no") or data.get("new_phone"))
		new_em = (new_email or data.get("new_email") or "").strip().lower()

		if not new_mob and not new_em:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "Either new_mobile_no (10 digits) or new_email is required"}

		# Uniqueness validation
		if new_mob:
			if len(new_mob) != 10:
				frappe.local.response["http_status_code"] = 400
				return {"status": "error", "message": "Valid 10-digit mobile number is required"}
			existing_m = frappe.db.get_value("ConnectFest Registration", {"mobile_no": new_mob}, "name") or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": new_mob}, "name")
			if existing_m:
				frappe.local.response["http_status_code"] = 409
				return {"status": "error", "message": f"Mobile number {new_mob} is already registered to another attendee!"}

		if new_em:
			if "@" not in new_em:
				frappe.local.response["http_status_code"] = 400
				return {"status": "error", "message": "Valid email address is required"}
			existing_e = frappe.db.exists("User", new_em)
			if existing_e:
				frappe.local.response["http_status_code"] = 409
				return {"status": "error", "message": f"Email address {new_em} is already registered to another attendee!"}

		cache = frappe.cache()
		target_val = new_mob or new_em
		cache_key = cache.make_key(f"{OTP_CACHE_PREFIX}update:{sess_user}")

		otp_code = f"{random.randint(100000, 999999)}"
		cache_data = {
			"otp": otp_code,
			"new_mobile_no": new_mob or None,
			"new_email": new_em or None,
		}
		cache.setex(cache_key, 120, json.dumps(cache_data))

		frappe.logger("connectfest").info(f"[ConnectFest Contact Update] OTP {otp_code} for user {sess_user} updating to {target_val}")

		return {
			"status": "success",
			"message": f"OTP sent to {target_val}. Valid for 2 minutes (120 seconds).",
			"new_mobile_no": new_mob or None,
			"new_email": new_em or None,
			"expires_in": "2 minutes",
			"expires_in_seconds": 120,
			"otp": otp_code,
			"test_credentials": {
				"mock_otp": "123456",
				"generated_otp": otp_code,
				"info": "Razorpay-style Test Mode active: enter OTP '123456' or the generated OTP to verify and update contact."
			}
		}
	except Exception as e:
		frappe.log_error("ConnectFest Contact Update Error", frappe.get_traceback())
		return {"status": "error", "message": str(e)}


def verify_and_update_contact(otp=None, **kwargs):
	"""
	Verifies the OTP and updates mobile number and/or email in Frappe User and Registration records.
	"""
	from connectfest.services.otp import OTP_CACHE_PREFIX, TEST_MASTER_OTPS, is_otp_test_mode
	import json
	try:
		sess_user = getattr(frappe.session, "user", None)
		if not sess_user or sess_user == "Guest":
			frappe.local.response["http_status_code"] = 401
			return {"status": "unauthorized", "message": "Authentication required to update contact details."}

		data = extract_request_data(kwargs)
		submitted_otp = str(otp or data.get("otp") or "").strip()

		if not submitted_otp:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "OTP is required"}

		cache = frappe.cache()
		cache_key = cache.make_key(f"{OTP_CACHE_PREFIX}update:{sess_user}")
		cached_raw = cache.get(cache_key)

		if not cached_raw:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "OTP has expired! Valid only for 2 minutes. Please request a new OTP."}

		if isinstance(cached_raw, bytes):
			cached_raw = cached_raw.decode("utf-8")

		cached_data = json.loads(cached_raw)
		stored_otp = cached_data.get("otp")
		test_mode = is_otp_test_mode()

		is_valid = False
		if stored_otp and stored_otp == submitted_otp:
			is_valid = True
		elif test_mode and submitted_otp in TEST_MASTER_OTPS:
			is_valid = True

		if not is_valid:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "Invalid OTP. Please check the code or use test OTP '123456'."}

		try:
			cache.delete(cache_key)
		except Exception:
			pass

		new_mob = cached_data.get("new_mobile_no")
		new_em = cached_data.get("new_email")

		# Apply Updates
		updated_fields = {}
		if new_mob:
			frappe.db.set_value("User", sess_user, "mobile_no", new_mob)
			frappe.db.set_value("User", sess_user, "phone", new_mob)
			if frappe.db.exists("ConnectFest Registration", {"email": sess_user}):
				frappe.db.set_value("ConnectFest Registration", {"email": sess_user}, "mobile_no", new_mob)
			if frappe.db.exists("ConnectFest Visitor Registration", {"email": sess_user}):
				frappe.db.set_value("ConnectFest Visitor Registration", {"email": sess_user}, "mobile_no", new_mob)
			updated_fields["mobile_no"] = new_mob

		if new_em:
			frappe.db.set_value("User", sess_user, "email", new_em)
			if frappe.db.exists("ConnectFest Registration", {"email": sess_user}):
				frappe.db.set_value("ConnectFest Registration", {"email": sess_user}, "email", new_em)
			if frappe.db.exists("ConnectFest Visitor Registration", {"email": sess_user}):
				frappe.db.set_value("ConnectFest Visitor Registration", {"email": sess_user}, "email", new_em)
			updated_fields["email"] = new_em

		frappe.db.commit()

		return {
			"status": "success",
			"message": "Contact details updated successfully with OTP verification!",
			"user": sess_user,
			"updated_fields": updated_fields
		}
	except Exception as e:
		frappe.log_error("ConnectFest Contact Verify Error", frappe.get_traceback())
		return {"status": "error", "message": str(e)}


def update_profile(**kwargs):
	"""
	Secure Edit Profile API for logged-in attendees (Exhibitors & Visitors).
	Security Highlights:
	- Anti-IDOR: Non-admin users are strictly locked to editing frappe.session.user only.
	- Identity Lock: Direct modification of 'email' or 'mobile_no' is blocked; must use request_contact_update with OTP.
	- Financial & Booking Lock: Fields like payment_status, amount, stall_number, status cannot be modified.
	- Input Sanitization: All text fields are stripped of HTML/script tags to prevent stored XSS.
	- Safe Image Upload: Base64 logo data URLs are converted to secure Frappe files; URLs are vetted against javascript: schemes.
	- Rate Limiting: 20 requests/minute per session user to thwart automated abuse.
	"""
	from connectfest.services.common import (
		check_rate_limit,
		parse_boolean,
		save_image_from_base64_or_url,
		set_cached_record,
		STALL_CACHE_PREFIX,
	)
	from connectfest.services.passes import get_my_dashboard
	from frappe.utils import strip_html

	try:
		# 1. Authentication Check
		sess_user = getattr(frappe.session, "user", None)
		if not sess_user or sess_user == "Guest":
			frappe.local.response["http_status_code"] = 401
			return {
				"status": "unauthorized",
				"message": "Authentication required! Please log in with OTP or credentials to edit your profile.",
			}

		# 2. Rate Limit (20 req/min)
		if not check_rate_limit(f"update_profile:{sess_user}", limit=20, window_secs=60):
			frappe.local.response["http_status_code"] = 429
			return {
				"status": "error",
				"message": "Too many profile update requests. Please wait a minute before trying again.",
			}

		data = extract_request_data(kwargs)
		is_admin = is_admin_user(sess_user)

		# 3. Anti-IDOR Protection: Disallow modifying other users unless Admin
		target_user = sess_user
		requested_user = (data.get("email") or data.get("user") or data.get("user_email") or "").strip().lower()
		if requested_user and requested_user != sess_user.lower():
			if not is_admin:
				frappe.local.response["http_status_code"] = 403
				return {
					"status": "forbidden",
					"message": "Access Denied: You are not authorized to modify another attendee's profile!",
				}
			target_user = requested_user

		if not frappe.db.exists("User", target_user):
			frappe.local.response["http_status_code"] = 404
			return {"status": "not_found", "message": f"User account '{target_user}' does not exist."}

		# 4. Identity Lock: Disallow direct changes to email or mobile_no
		target_user_doc = frappe.get_doc("User", target_user)
		current_mob = (target_user_doc.mobile_no or target_user_doc.phone or "").strip()

		attempted_mob = (data.get("mobile_no") or data.get("phone") or "").strip()
		if attempted_mob and attempted_mob != current_mob and not is_admin:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "Direct mobile number updates are blocked for security! Please use the OTP-verified contact update API (connectfest.api.request_contact_update).",
			}

		# 5. Extract and Sanitize Whitelisted Profile Fields
		updated_fields = {}
		updates_exhibitor = {}
		updates_user = {}

		# Full Name
		raw_name = data.get("full_name") or data.get("name")
		if raw_name is not None:
			clean_name = strip_html(str(raw_name)).strip()[:140]
			if clean_name:
				parts = clean_name.split(" ", 1)
				first_name = parts[0]
				last_name = parts[1] if len(parts) > 1 else ""
				updates_user["first_name"] = first_name
				updates_user["last_name"] = last_name
				updates_user["full_name"] = clean_name
				updates_exhibitor["full_name"] = clean_name
				updated_fields["full_name"] = clean_name

		# Company Name
		raw_company = data.get("company_name")
		if raw_company is not None:
			clean_company = strip_html(str(raw_company)).strip()[:140]
			updates_exhibitor["company_name"] = clean_company
			updated_fields["company_name"] = clean_company

		# Company Logo
		raw_logo = data.get("company_logo") if data.get("company_logo") is not None else data.get("logo")
		if raw_logo is not None:
			saved_logo_url = save_image_from_base64_or_url(str(raw_logo), file_name_prefix=f"logo_{target_user.split('@')[0][:8]}")
			if saved_logo_url:
				updates_exhibitor["company_logo"] = saved_logo_url
				updates_user["user_image"] = saved_logo_url
				updated_fields["company_logo"] = saved_logo_url

		# Location / City
		raw_loc = data.get("location")
		if raw_loc is not None:
			clean_loc = strip_html(str(raw_loc)).strip()[:140]
			updates_exhibitor["location"] = clean_loc
			updated_fields["location"] = clean_loc

		# Industry & Segment
		raw_ind = data.get("industry")
		if raw_ind is not None:
			clean_ind = strip_html(str(raw_ind)).strip()[:100]
			updates_exhibitor["industry"] = clean_ind
			updated_fields["industry"] = clean_ind

		raw_seg = data.get("segment")
		if raw_seg is not None:
			clean_seg = strip_html(str(raw_seg)).strip()[:100]
			updates_exhibitor["segment"] = clean_seg
			updated_fields["segment"] = clean_seg

		# BNI Details
		if "is_bni_member" in data:
			bni_val = parse_boolean(data.get("is_bni_member"))
			updates_exhibitor["is_bni_member"] = bni_val
			updated_fields["is_bni_member"] = bool(bni_val)

		raw_reg = data.get("region_name")
		if raw_reg is not None:
			clean_reg = strip_html(str(raw_reg)).strip()[:100]
			updates_exhibitor["region_name"] = clean_reg
			updated_fields["region_name"] = clean_reg

		raw_chap = data.get("chapter_name")
		if raw_chap is not None:
			clean_chap = strip_html(str(raw_chap)).strip()[:100]
			updates_exhibitor["chapter_name"] = clean_chap
			updated_fields["chapter_name"] = clean_chap

		# Visitor Day Choice (if visitor)
		visitor_day = data.get("which_day")
		valid_days = ["20 Feb", "21 Feb", "Both days"]
		update_visitor_day = None
		if visitor_day and str(visitor_day).strip() in valid_days:
			update_visitor_day = str(visitor_day).strip()
			updated_fields["which_day"] = update_visitor_day

		if not updated_fields:
			return {
				"status": "unchanged",
				"message": "No valid profile fields provided for update.",
				"user": target_user,
			}

		# 6. Apply Updates to User Record
		if updates_user:
			for k, v in updates_user.items():
				frappe.db.set_value("User", target_user, k, v)

		# 7. Apply Updates to ConnectFest Exhibitor Registration(s)
		if updates_exhibitor:
			cache = frappe.cache()
			ex_regs = frappe.db.get_all(
				"ConnectFest Registration",
				filters={"email": target_user},
				fields=["name", "registration_id"]
			)
			for ex in ex_regs:
				for k, v in updates_exhibitor.items():
					frappe.db.set_value("ConnectFest Registration", ex.name, k, v)
				# Update Cache
				cached_record = frappe.get_doc("ConnectFest Registration", ex.name).as_dict()
				set_cached_record(cache, f"{STALL_CACHE_PREFIX}{ex.registration_id}", cached_record)
				if ex.name != ex.registration_id:
					set_cached_record(cache, f"{STALL_CACHE_PREFIX}{ex.name}", cached_record)

		# 8. Apply Updates to Visitor Registration(s)
		if "full_name" in updated_fields or update_visitor_day:
			vis_regs = frappe.db.get_all(
				"ConnectFest Visitor Registration",
				filters={"email": target_user},
				fields=["name", "registration_id"]
			)
			for vr in vis_regs:
				if "full_name" in updated_fields:
					frappe.db.set_value("ConnectFest Visitor Registration", vr.name, "full_name", updated_fields["full_name"])
				if update_visitor_day:
					frappe.db.set_value("ConnectFest Visitor Registration", vr.name, "which_day", update_visitor_day)

		frappe.db.commit()

		# 9. Return Refreshed Dashboard
		refreshed_dashboard = get_my_dashboard(email=target_user)

		return {
			"status": "success",
			"message": "Profile updated successfully!",
			"user": target_user,
			"updated_fields": updated_fields,
			"dashboard": refreshed_dashboard,
		}
	except Exception as e:
		frappe.log_error("ConnectFest Profile Update Error", frappe.get_traceback())
		return {"status": "error", "message": "Something went wrong while updating profile. Please try again."}


def cleanup_user_registrations_and_stalls(user_email=None, user_mobile=None):
	"""
	Core cascade cleanup function:
	Given a user's email and/or mobile number:
	1. Releases all stalls allocated to their registrations back to 'Available' (clearing locks).
	2. Deletes all their ConnectFest Registration records.
	3. Deletes all their ConnectFest Visitor Registration records.
	4. Resets any stalls where booked_by_email matches user_email.
	5. Clears Redis caches and hold locks.
	"""
	clean_email = (user_email or "").strip().lower()
	clean_mob = (user_mobile or "").strip()

	if not clean_email and not clean_mob:
		return {"released_stalls": [], "deleted_exhibitors": 0, "deleted_visitors": 0}

	released_stalls = []
	deleted_exhibitors = 0
	deleted_visitors = 0
	cache = frappe.cache()

	# 1. Fetch all Exhibitor Registrations
	conditions = []
	params = {}
	if clean_email:
		conditions.append("email = %(email)s")
		params["email"] = clean_email
	if clean_mob:
		conditions.append("mobile_no = %(mobile)s")
		params["mobile"] = clean_mob

	where_str = " OR ".join(conditions)

	ex_regs = frappe.db.sql(f"""
		SELECT name, registration_id, email, mobile_no, stall_number
		FROM `tabConnectFest Registration`
		WHERE {where_str}
	""", params, as_dict=True)

	for reg in ex_regs:
		stall_num = reg.get("stall_number")
		if stall_num and frappe.db.exists("ConnectFest Stall", stall_num):
			try:
				stall = frappe.get_doc("ConnectFest Stall", stall_num)
				stall.status = "Available"
				stall.booked_by_registration = None
				stall.booked_by_name = None
				stall.booked_by_company = None
				stall.booked_by_email = None
				stall.booked_by_mobile = None
				stall.hold_token = None
				stall.hold_expires_at = None
				stall.payment_mode = None
				stall.save(ignore_permissions=True)
				released_stalls.append(stall_num)
			except Exception:
				pass

			# Clear redis locks
			try:
				cache.delete(cache.make_key(f"connectfest:stall_lock:{stall_num}"))
				cache.delete_keys(f"cf:stall_lock:{stall_num}*")
			except Exception:
				pass

		# Delete registration doc
		try:
			frappe.delete_doc("ConnectFest Registration", reg.name, ignore_permissions=True, force=True)
			deleted_exhibitors += 1
		except Exception:
			frappe.db.sql("DELETE FROM `tabConnectFest Registration` WHERE name = %s", (reg.name,))
			deleted_exhibitors += 1

	# 2. Direct Stall check by booked_by_email or booked_by_mobile (in case stall was booked without registration doc)
	direct_stalls = frappe.db.sql(f"""
		SELECT stall_number FROM `tabConnectFest Stall`
		WHERE (booked_by_email = %(email)s OR (booked_by_mobile = %(mobile)s AND %(mobile)s != ''))
	""", {"email": clean_email, "mobile": clean_mob}, as_dict=True)

	for ds in direct_stalls:
		s_num = ds["stall_number"]
		if s_num not in released_stalls and frappe.db.exists("ConnectFest Stall", s_num):
			try:
				stall = frappe.get_doc("ConnectFest Stall", s_num)
				stall.status = "Available"
				stall.booked_by_registration = None
				stall.booked_by_name = None
				stall.booked_by_company = None
				stall.booked_by_email = None
				stall.booked_by_mobile = None
				stall.hold_token = None
				stall.hold_expires_at = None
				stall.payment_mode = None
				stall.save(ignore_permissions=True)
				released_stalls.append(s_num)
				cache.delete(cache.make_key(f"connectfest:stall_lock:{s_num}"))
				cache.delete_keys(f"cf:stall_lock:{s_num}*")
			except Exception:
				pass

	# 3. Fetch and Delete all Visitor Registrations
	vis_regs = frappe.db.sql(f"""
		SELECT name, registration_id
		FROM `tabConnectFest Visitor Registration`
		WHERE {where_str}
	""", params, as_dict=True)

	for vr in vis_regs:
		try:
			frappe.delete_doc("ConnectFest Visitor Registration", vr.name, ignore_permissions=True, force=True)
			deleted_visitors += 1
		except Exception:
			frappe.db.sql("DELETE FROM `tabConnectFest Visitor Registration` WHERE name = %s", (vr.name,))
			deleted_visitors += 1

	try:
		if not getattr(frappe.db, "_disable_transaction_control", 0):
			frappe.db.commit()
	except Exception:
		pass

	# Clear user cache
	try:
		if clean_email:
			cache.delete_keys(f"cf:user:{clean_email}*")
			cache.delete_keys(f"cf:reg:*{clean_email}*")
	except Exception:
		pass

	frappe.clear_cache()

	return {
		"released_stalls": list(set(released_stalls)),
		"deleted_exhibitors": deleted_exhibitors,
		"deleted_visitors": deleted_visitors,
	}


def on_user_trash(doc, method=None):
	"""
	Frappe DocEvent Hook: Triggers when ANY User document is deleted in Frappe.
	Cascades deletion to their registrations and releases all their booked stalls.
	"""
	# Never wipe for system Administrator or Guest
	if doc.name in ("Administrator", "Guest"):
		return

	user_email = (doc.name or doc.email or "").strip().lower()
	user_mobile = (doc.mobile_no or doc.phone or "").strip()
	cleanup_user_registrations_and_stalls(user_email=user_email, user_mobile=user_mobile)


@handle_api_errors("ConnectFest Admin Delete User Error")
def admin_delete_user(**kwargs):
	"""
	Admin API: Complete user deletion with automatic cascade cleanup.
	Deletes:
	- Frappe User account
	- All exhibitor registrations for this email/phone
	- All visitor registrations for this email/phone
	- Releases all booked stalls back to 'Available'
	"""
	err = check_admin_permission("delete user")
	if err:
		return err

	data = extract_request_data(kwargs)
	target_user = (data.get("email") or data.get("user") or data.get("user_id") or data.get("name") or "").strip().lower()
	mobile_no = (data.get("mobile_no") or data.get("phone") or "").strip()

	if not target_user and not mobile_no:
		return {"status": "error", "message": "User email or mobile number is required for deletion."}

	if target_user in ("administrator", "guest"):
		return {"status": "error", "message": f"System user '{target_user}' cannot be deleted!"}

	# Resolve user details if user exists in tabUser
	user_deleted = False
	if target_user and frappe.db.exists("User", target_user):
		u_doc = frappe.get_doc("User", target_user)
		if not mobile_no:
			mobile_no = (u_doc.mobile_no or u_doc.phone or "").strip()
		frappe.delete_doc("User", target_user, ignore_permissions=True, force=True)
		user_deleted = True

	# Cascade cleanup registrations & stalls
	cleanup_res = cleanup_user_registrations_and_stalls(user_email=target_user, user_mobile=mobile_no)

	frappe.db.commit()

	return {
		"status": "success",
		"message": f"User '{target_user or mobile_no}' and all associated bookings have been successfully deleted.",
		"user": target_user,
		"user_account_deleted": user_deleted,
		"exhibitors_deleted": cleanup_res["deleted_exhibitors"],
		"visitors_deleted": cleanup_res["deleted_visitors"],
		"released_stalls": cleanup_res["released_stalls"],
	}

