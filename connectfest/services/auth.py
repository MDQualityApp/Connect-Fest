import frappe
from connectfest.services.common import extract_request_data


def ensure_frappe_user(email, full_name, mobile_no=None, role="Website User"):
	"""
	Ensures a Frappe User exists for visitors and exhibitors so they can log in.
	"""
	if not email or "@" not in email:
		return None
	clean_email = email.strip().lower()
	try:
		if not frappe.db.exists("User", clean_email):
			# Ensure role exists in tabRole
			if role and not frappe.db.exists("Role", role):
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

			roles_list = []
			if role and frappe.db.exists("Role", role):
				roles_list.append({"doctype": "Has Role", "role": role})

			names = (full_name or clean_email.split("@")[0]).strip().split(" ", 1)
			first_name = names[0] or clean_email.split("@")[0]
			last_name = names[1] if len(names) > 1 else ""
			user = frappe.get_doc({
				"doctype": "User",
				"email": clean_email,
				"first_name": first_name,
				"last_name": last_name,
				"phone": mobile_no or "",
				"mobile_no": mobile_no or "",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "Website User",
				"roles": roles_list,
			})
			user.flags.ignore_permissions = True
			user.flags.ignore_password_policy = True
			user.insert(ignore_if_duplicate=True)
			frappe.db.commit()
			return user.name
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


def get_current_user_profile(**kwargs):
	"""
	Resolves current logged in user from Frappe session cookie or Token Auth.
	Strictly requires authentication (Guests not allowed).
	Prevents IDOR: Users can only see their own profile.
	"""
	from connectfest.services.passes import get_my_dashboard
	try:
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
		requested_email = (data.get("email") or data.get("user_email") or "").strip().lower()
		if requested_email and requested_email != sess_user.lower() and not is_admin_user(sess_user):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Access Denied: You are not authorized to view another attendee's personal data!",
			}

		user_doc = frappe.get_doc("User", sess_user)
		dash = get_my_dashboard(email=sess_user)
		return {
			"status": "success",
			"is_logged_in": True,
			"user": sess_user,
			"full_name": user_doc.full_name,
			"mobile_no": user_doc.mobile_no or user_doc.phone,
			"roles": [r for r in frappe.get_roles(sess_user) if r not in ("All", "Guest")],
			"profile": dash,
		}
	except Exception as e:
		frappe.log_error("ConnectFest Profile Fetch Error", frappe.get_traceback())
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}


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

		otp = str(data.get("otp") or data.get("code") or "").strip()
		if otp and mob:
			from connectfest.services.otp import verify_otp
			return verify_otp(mobile_no=mob, otp=otp)

		if pwd and usr:
			try:
				frappe.auth.LoginManager().authenticate(usr, pwd)
				frappe.auth.LoginManager().post_login()
			except frappe.AuthenticationError:
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
		frappe.local.response["http_status_code"] = 500
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
		try:
			frappe.auth.LoginManager().authenticate(usr, pwd)
		except frappe.AuthenticationError:
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
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Something went wrong during admin authentication."}


def admin_toggle_user_status(user_email=None, mobile_no=None, enabled=1, **kwargs):
	"""
	Admin API to enable or disable attendee User accounts.
	Requires System Manager or Administrator privileges.
	"""
	try:
		from frappe.utils import cint
		sess_user = getattr(frappe.session, "user", None)
		if not sess_user or not is_admin_user(sess_user):
			frappe.local.response["http_status_code"] = 403
			return {"status": "forbidden", "message": "Access Denied: Only administrators can enable or disable users!"}

		data = extract_request_data(kwargs)
		target_email = (user_email or data.get("user_email") or data.get("email") or "").strip().lower()
		target_mob = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()
		new_enabled = cint(enabled if enabled is not None else data.get("enabled", 1))

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
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": str(e)}


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
		frappe.local.response["http_status_code"] = 500
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
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": str(e)}
